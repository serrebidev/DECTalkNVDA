# Driver-level tests: cancellation and the startup hold.
#
# These exercise the real SynthDriver (and the real engine) against a fake
# WavePlayer that models an audio device -- a bounded buffer that plays out in
# real time and is emptied by stop(). Because the fake keeps the playback
# timeline, a test can ask what was actually *heard*, which is the only way to
# tell "we handed the device a slice and immediately flushed it" apart from
# "a fragment of a cancelled announcement was spoken over the next one".
#
# NVDA itself is stubbed: the driver only needs config, nvwave, logHandler,
# autoSettingsUtils.driverSetting, speech.commands and synthDriverHandler.

import os
import sys
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.environ.get("DT_ADDON") or os.path.join(os.path.dirname(HERE), "addon")

SR = 11025


# ---------------------------------------------------------------- NVDA stubs
class _Conf(dict):
	spec = {}


config = types.ModuleType("config")
config.conf = _Conf({
	"audio": {"outputDevice": "default"},
	"speech": {"outputDevice": "default"},
	"dectalknew": {"customVoices": "{}"},
})
sys.modules["config"] = config


class FakePlayer:
	"""An audio device: a bounded buffer that plays out in real time."""

	BUFFER_SEC = 0.4

	def __init__(self, **kwargs):
		self.lock = threading.Lock()
		self.fed = []           # (acceptedAt, nbytes) handed to the device
		self.segs = []          # [playStart, playEnd], truncated by stop()
		self.playedUntil = None
		self.stops = []
		self.closed = False

	def _queuedSec(self, now):
		if self.playedUntil is None:
			return 0.0
		return max(0.0, self.playedUntil - now)

	def feed(self, pcm, onDone=None):
		secs = len(pcm) / 2.0 / SR
		while True:
			now = time.perf_counter()
			with self.lock:
				queued = self._queuedSec(now)
				# A chunk bigger than the whole buffer is accepted once the
				# device has drained, as a real WavePlayer eventually does.
				if queued + secs <= self.BUFFER_SEC or queued <= 0:
					base = max(now, self.playedUntil or now)
					self.playedUntil = base + secs
					self.fed.append((now, len(pcm)))
					self.segs.append([base, base + secs])
					break
			time.sleep(0.005)
		if onDone is not None:
			onDone()

	def stop(self):
		with self.lock:
			now = time.perf_counter()
			self.stops.append(now)
			self.playedUntil = None
			kept = []
			for begin, end in self.segs:
				if begin >= now:
					continue  # never started playing: the flush discards it
				kept.append([begin, min(end, now)])
			self.segs = kept

	def idle(self):
		while True:
			with self.lock:
				left = self._queuedSec(time.perf_counter())
			if left <= 0:
				return
			time.sleep(min(left, 0.02))

	def pause(self, switch):
		pass

	def close(self):
		self.closed = True

	# -- measurement
	def heardAfter(self, when):
		"""Seconds of audio that actually reached the ears after `when`."""
		with self.lock:
			now = time.perf_counter()
			return sum(
				max(0.0, min(end, now) - max(begin, when))
				for begin, end in self.segs
			)

	def bytesFedAfter(self, when):
		with self.lock:
			return sum(n for at, n in self.fed if at > when)

	def totalBytes(self):
		with self.lock:
			return sum(n for _, n in self.fed)

	def firstFeedTime(self):
		with self.lock:
			return self.fed[0][0] if self.fed else None

	def reset(self):
		with self.lock:
			self.fed = []
			self.segs = []
			self.playedUntil = None
			self.stops = []


nvwave = types.ModuleType("nvwave")
nvwave.WavePlayer = FakePlayer
sys.modules["nvwave"] = nvwave


class _Log:
	DEBUG = 10
	debugEnabled = False
	#: Stands in for NVDA's synchronous log file write, whose cost is what
	#: made cancel() slow enough to leak audio.
	writeDelay = 0.0

	def isEnabledFor(self, level):
		return self.debugEnabled

	def debug(self, msg, *args):
		if self.debugEnabled and self.writeDelay:
			time.sleep(self.writeDelay)

	def info(self, msg, *args):
		pass

	def warning(self, msg, *args):
		pass

	def error(self, msg, *args):
		print("ERROR: %s" % (msg,))

	def exception(self, msg, *args):
		import traceback
		print("EXC: %s" % (msg,))
		traceback.print_exc()


logHandler = types.ModuleType("logHandler")
logHandler.log = _Log()
sys.modules["logHandler"] = logHandler


class _Setting:
	def __init__(self, *args, **kwargs):
		self.id = args[0] if args else kwargs.get("id")


driverSetting = types.ModuleType("autoSettingsUtils.driverSetting")
driverSetting.BooleanDriverSetting = _Setting
driverSetting.NumericDriverSetting = _Setting
driverSetting.DriverSetting = _Setting
autoSettingsUtils = types.ModuleType("autoSettingsUtils")
autoSettingsUtils.driverSetting = driverSetting
sys.modules["autoSettingsUtils"] = autoSettingsUtils
sys.modules["autoSettingsUtils.driverSetting"] = driverSetting

commands = types.ModuleType("speech.commands")
for _name in (
	"BreakCommand", "CharacterModeCommand", "IndexCommand", "LangChangeCommand",
	"PitchCommand", "RateCommand", "VolumeCommand",
):
	setattr(commands, _name, type(_name, (), {}))
speech = types.ModuleType("speech")
speech.commands = commands
sys.modules["speech"] = speech
sys.modules["speech.commands"] = commands


class _Notification:
	def __init__(self):
		self.events = []

	def notify(self, **kwargs):
		self.events.append(kwargs)


class _BaseSynthDriver:
	pass


for _name in (
	"VoiceSetting", "RateSetting", "RateBoostSetting", "PitchSetting",
	"InflectionSetting", "VolumeSetting",
):
	setattr(_BaseSynthDriver, _name, staticmethod(
		(lambda n: lambda *a, **kw: _Setting(n))(_name)
	))


synthDriverHandler = types.ModuleType("synthDriverHandler")
synthDriverHandler.SynthDriver = _BaseSynthDriver
synthDriverHandler.VoiceInfo = type("VoiceInfo", (), {
	"__init__": lambda self, *a, **kw: None,
})
synthDriverHandler.synthDoneSpeaking = _Notification()
synthDriverHandler.synthIndexReached = _Notification()
sys.modules["synthDriverHandler"] = synthDriverHandler

sys.path.insert(0, ADDON)
sys.path.insert(0, os.path.join(ADDON, "synthDrivers"))

from synthDrivers import dectalknew  # noqa: E402

SynthDriver = dectalknew.SynthDriver
log = logHandler.log
doneSpeaking = synthDriverHandler.synthDoneSpeaking
indexReached = synthDriverHandler.synthIndexReached

_failures = []


def check(name, ok, detail=""):
	print(("PASS  " if ok else "FAIL  ") + name + ("  " + detail if detail else ""))
	if not ok:
		_failures.append(name)


def waitQuiet(driver, timeout=30.0):
	end = time.perf_counter() + timeout
	while time.perf_counter() < end:
		if not driver._busy and driver._queue.empty() and driver._doneQueue.empty():
			driver._player.idle()
			if not driver._busy and driver._queue.empty():
				return True
		time.sleep(0.01)
	return False


def waitFirstFeed(player, timeout=8.0):
	end = time.perf_counter() + timeout
	while player.firstFeedTime() is None and time.perf_counter() < end:
		time.sleep(0.002)
	return player.firstFeedTime()


# ------------------------------------------- a cancel must silence instantly
def test_nothing_heard_after_cancel(writeDelay, label):
	"""Not one sample of a cancelled utterance may reach the ears.

	feed() blocks on the device's backpressure, so a cancel arriving while it
	is blocked lands *before* a slice the driver then hands over -- that slice
	goes in behind cancel's stop() and is the only thing the user hears of an
	announcement NVDA had already killed. `writeDelay` reproduces the other
	half of it: cancel() used to build its debug log line before bumping the
	generation, so a slow log write left the feed path free to keep going.
	"""
	driver = SynthDriver()
	player = driver._player
	log.debugEnabled = True
	log.writeDelay = writeDelay
	try:
		driver.speak(["The quick brown fox jumps over the lazy dog, again and again."])
		deadline = time.perf_counter() + 8
		while player.totalBytes() < 4000 and time.perf_counter() < deadline:
			time.sleep(0.005)
		cancelAt = time.perf_counter()
		driver.cancel()
		time.sleep(0.6)
		heard = player.heardAfter(cancelAt)
		handed = player.bytesFedAfter(cancelAt)
		check(
			"nothing heard after cancel (%s)" % label,
			heard < 0.010,
			"%.0f ms heard, %.0f ms handed over" % (
				heard * 1000, handed / 2.0 / SR * 1000),
		)
		check(
			"at most one slice handed over after cancel (%s)" % label,
			handed <= SynthDriver.SLICE_BYTES,
			"%d bytes" % handed,
		)
	finally:
		log.debugEnabled = False
		log.writeDelay = 0.0
		driver.terminate()


def test_cancel_storm():
	"""alt+tab held down: cancel/speak faster than anything can finish."""
	driver = SynthDriver()
	player = driver._player
	log.debugEnabled = True
	log.writeDelay = 0.02
	worst = 0.0
	try:
		for i in range(12):
			driver.cancel()
			after = time.perf_counter()
			time.sleep(0.02)
			worst = max(worst, player.heardAfter(after))
			driver.speak(["Window number %d, application frame, list item" % i])
			time.sleep(0.16)
		driver.cancel()
		after = time.perf_counter()
		time.sleep(0.05)
		worst = max(worst, player.heardAfter(after))
	finally:
		log.debugEnabled = False
		log.writeDelay = 0.0
	check("nothing heard after any cancel in a 12-cancel storm",
		  worst < 0.010, "worst %.0f ms" % (worst * 1000))
	driver.terminate()


# ----------------------------------------------------------- startup hold
def test_startup_hold():
	"""The hold is paid once per generation, i.e. once per cancel.

	It exists so an announcement NVDA kills ~150 ms after asking for it is
	never heard at all; every such announcement is preceded by a cancel. Later
	utterances of the same burst are a continuation NVDA is committed to, and
	holding those inserts dead air whenever the pipeline has run dry.
	"""
	driver = SynthDriver()
	player = driver._player

	driver.cancel()
	player.reset()
	asked = time.perf_counter()
	driver.speak(["Hello there"])
	delay = (waitFirstFeed(player) - asked) * 1000
	check("first utterance after a cancel is held",
		  160 <= delay <= 400, "%.0f ms" % delay)
	waitQuiet(driver)

	# Device drained, no cancel in between: this must start immediately.
	player.reset()
	time.sleep(0.05)
	asked = time.perf_counter()
	driver.speak(["Second part"])
	delay = (waitFirstFeed(player) - asked) * 1000
	check("later utterance of the same burst is not held",
		  delay < 150, "%.0f ms" % delay)
	waitQuiet(driver)

	# The next cancel arms it again.
	driver.cancel()
	player.reset()
	asked = time.perf_counter()
	driver.speak(["Third"])
	delay = (waitFirstFeed(player) - asked) * 1000
	check("the hold re-arms after the next cancel",
		  160 <= delay <= 400, "%.0f ms" % delay)
	waitQuiet(driver)
	driver.terminate()


def test_killed_inside_hold():
	driver = SynthDriver()
	player = driver._player
	driver.cancel()
	player.reset()
	driver.speak(["Window title that should never be heard"])
	time.sleep(0.15)
	driver.cancel()
	time.sleep(0.5)
	check("an announcement cancelled inside the hold is silent",
		  player.totalBytes() == 0, "%d bytes" % player.totalBytes())
	driver.terminate()


# ------------------------------------------------------ continuous reading
def test_continuous_reading():
	driver = SynthDriver()
	player = driver._player
	del indexReached.events[:]
	del doneSpeaking.events[:]
	driver.cancel()
	player.reset()
	texts = [
		"Chapter one. " * 3,
		"It was a bright cold day. " * 3,
		"The clocks were striking thirteen. " * 3,
		"Winston Smith slipped quickly through the doors. " * 3,
	]
	started = time.perf_counter()
	for i, text in enumerate(texts):
		mark = commands.IndexCommand()
		mark.index = i + 1
		driver.speak([mark, text])
	finished = waitQuiet(driver, timeout=60)
	time.sleep(0.4)  # let the completion thread settle
	audio = player.totalBytes() / 2.0 / SR
	with player.lock:
		fed = list(player.fed)
	steps = [b[0] - a[0] for a, b in zip(fed, fed[1:])]
	biggest = max(steps) * 1000 if steps else 0.0
	check("continuous reading completes", finished)
	check("continuous reading produced audio", audio > 5.0, "%.2f s" % audio)
	check("the device is handed audio in small steps",
		  biggest < 250, "largest step %.0f ms" % biggest)
	marks = [kw["index"] for kw in indexReached.events]
	check("index marks arrive in order",
		  marks == sorted(marks) and len(marks) == len(texts), str(marks))
	check("exactly one synthDoneSpeaking for the burst",
		  len(doneSpeaking.events) == 1, str(len(doneSpeaking.events)))
	print("      %d utterances, %.2f s of audio in %.2f s wall clock"
		  % (len(texts), audio, time.perf_counter() - started))
	driver.terminate()


test_nothing_heard_after_cancel(0.0, "fast log")
test_nothing_heard_after_cancel(0.25, "slow log write")
test_cancel_storm()
test_startup_hold()
test_killed_inside_hold()
test_continuous_reading()

print()
if _failures:
	print("FAILURES: %s" % (_failures,))
	sys.exit(1)
print("ALL DRIVER TESTS PASSED")
