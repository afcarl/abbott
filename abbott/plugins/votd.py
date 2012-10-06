import random
from collections import defaultdict
from functools import wraps
import datetime
import time

from twisted.internet import reactor, defer
from twisted.python import log

try:
    from pretty import date as prettydate
except ImportError:
    print "Please install the pypi package 'py-pretty'"
    raise

from ..command import CommandPluginSuperclass
from ..transport import Event

def find_time_until(hour_minute):
    """Returns a datetime.timedelta for the time interval between now and the
    next time of the given hour, in the current locale

    """
    today = datetime.date.today()

    hour = datetime.time(hour=hour_minute[0], minute=hour_minute[1])

    targetdt = datetime.datetime.combine(today, hour)
    if targetdt <= datetime.datetime.now():
        tomorrow = today + datetime.timedelta(days=1)
        targetdt = datetime.datetime.combine(tomorrow, hour)

    timeuntil = targetdt - datetime.datetime.now()
    return timeuntil

def td_to_str(td):
    """Takes a timedelta and returns a string describing the interval as if it
    were taking place at a point in the future from now

    """
    return prettydate(
            datetime.datetime.now() + td
            )

def delay(t):
    """"This should only be used for short delays because long delays won't be
    canceled properly on plugin unload or other exceptional cases

    """
    d = defer.Deferred()
    reactor.callLater(t, d.callback, None)
    return d

def require_channel(func):
    """Wraps command callbacks and requires them to be in response to a channel
    message, not a private message directed to the bot.

    """
    @wraps(func)
    def newfunc(self, event, match):
        if event.direct:
            event.reply("Hey, you can't do that in here!")
        else:
            return func(self, event, match)
    return newfunc

class VoiceOfTheDay(CommandPluginSuperclass):
    def __init__(self, *args):
        self.started = False
        self.timer = None
        super(VoiceOfTheDay, self).__init__(*args)

    def start(self):
        super(VoiceOfTheDay, self).start()

        votdgroup = self.install_cmdgroup(
                grpname="votd",
                permission="vott.configure",
                helptext="Voice of the Day configuration commands",
                )

        votdgroup.install_command(
                cmdname="enable",
                callback=self.enable,
                helptext="Turns on votd for this channel",
                )
        votdgroup.install_command(
                cmdname="disable",
                callback=self.disable,
                helptext="Turns off votd for this channel",
                )

        votdgroup.install_command(
                cmdname="draw",
                callback=self.draw,
                helptext="Draws the raffle right now",
                )

        votdgroup.install_command(
                cmdname="settime",
                cmdusage="<hour:minute>",
                argmatch=r"(?P<hour>\d+)(?:[:](?P<minute>\d+))$",
                callback=self.settime,
                helptext="Sets which hour the drawing will happen, in the current locale",
                )

        # Don't forget!
        self._set_timer()
        self.started = True

        # last spoken time keeps track of channel idleness. We don't want to
        # interrupt conversation, so we see when the last time anyone spoke and
        # will only do a drawing if nobody is talking
        self.lastspoken = 0

    def stop(self):
        if self.timer:
            self.timer.cancel()

    def reload(self):
        super(VoiceOfTheDay, self).reload()

        # maps nicks to counts
        self.config["counter"] = defaultdict(int, self.config.get("counter", {}))

        # The channel we're doing this in, or None for disabled
        self.config['channel'] = self.config.get('channel', None)

        # The hour of the day to do the drawing
        self.config['hour'] = tuple(self.config.get('hour', (0,0)))

        # Who currently has voice due to this plugin, right now. Will be
        # devoiced at the next drawing
        self.config['currentvoice'] = self.config.get("currentvoice", None)

        # Who had voice on the last drawing. Is ineligible for the next
        # drawing.
        self.config['lastvoice'] = self.config.get("lastvoice", None)

        # The time the last drawing occurred.
        self.config['lastdrawing'] = self.config.get('lastdrawing', None)

        # reset the timer, in case the hour in the config was changed manually
        if self.started:
            self._set_timer()

    def _set_timer(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None

        channel = self.config["channel"]
        if channel:
            timeuntil = find_time_until(self.config['hour'])
            self.timer = reactor.callLater(
                    max(int(timeuntil.total_seconds()), 5),
                    self._timer_up,
                    )
        
    @require_channel
    def enable(self, event, match):
        channel = event.channel
        self.config["channel"] = channel
        self.config.save()
        self._set_timer()
        event.reply("Done. Next scheduled drawing is {0}".format(
            td_to_str(
                find_time_until(self.config['hour'])
            )))

    @require_channel
    def disable(self, event, match):
        channel = event.channel
        self.config["channel"] = None
        self.config.save()
        self._set_timer()
        event.reply("Voice of the Day disabled for {0}".format(channel))

    @require_channel
    def settime(self, event, match):
        channel = event.channel
        hour = int(match.groupdict()['hour'])
        minute = int(match.groupdict().get('minute', None) or 0)
        if hour < 0 or hour > 23:
            event.reply("What kind of hour is that?")
            return
        if minute < 0 or minute > 59:
            event.reply("What kind of minute is that?")
            return
        self.config['hour'] = (hour, minute)
        self.config.save()
        self._set_timer()

        event.reply("VOTD drawing {3} happen at {0}:{1}, which is {2}".format(
            hour, minute,
            td_to_str(find_time_until((hour,minute))),
            "will" if self.config['channel'] else "would",
            ))


    @defer.inlineCallbacks
    def _send_as_op(self, event, reply=lambda s: None):
        """Issues an ircadmin.opself request, then sends the event. If the
        opself fails, sends an error to the reply function provided

        """
        try:
            yield self.transport.issue_request("ircadmin.opself", event.channel)
        except OpTimedOut:
            reply("I could not become OP. Check the error log, configuration, etc.")
            defer.returnValue(False)
        except NoOpMethod:
            reply("I can't do that in %s, I don't have OP and have no way to acquire it!" % event.channel)
            defer.returnValue(False)
        else:
            self.transport.send_event(event)
            defer.returnValue(True)

    def _timer_up(self):
        self.timer = None

        IDLE_TIME = 60*5

        now = time.time()
        if now - self.lastspoken >= IDLE_TIME:
            self._do_votd()
        else:
            towait = IDLE_TIME - (now - self.lastspoken)
            log.msg("Was going to do VOTD but the channel is active. Waiting %s seconds and trying again"%towait)
            self.timer = reactor.callLater(towait, self._timer_up)

    @defer.inlineCallbacks
    def _do_votd(self, channel=None):
        # Assume the timer is already None or has fired to call this method
        channel = self.config["channel"] or channel
        if not channel:
            raise RuntimeError("_do_votd() was called, but no channel defined")
        log.msg("Doing VOTD for %s" % channel)
        def say(msg):
            self.transport.send_event(Event("irc.do_msg",
                user=channel,
                message=msg,
                ))

        names = (yield self.transport.issue_request("irc.names", channel))

        # de-voice the current voice if he/she still has it
        currentvoice = self.config["currentvoice"]
        if currentvoice:
            if "+"+currentvoice in (names):
                e = Event("irc.do_mode",
                        channel=channel,
                        set=False,
                        modes="v",
                        user=currentvoice,
                        )
                if not (yield self._send_as_op(e)):
                    log.msg("Error while un-voicing previous voice. Bailing")
                    return



        counter = self.config["counter"]
        self.config['counter'] = defaultdict(int)

        # don't count the user that had voice just now
        if currentvoice in counter:
            del counter[currentvoice]

        # or the time before
        lastvoice = self.config["lastvoice"]
        if lastvoice and lastvoice in counter:
            del counter[lastvoice]

        # don't count any user that isn't actually here
        names = set(
                (x[1:] if x.startswith("@") or x.startswith("+")
                    else x)
                for x in names
                )
        for contestant in counter.keys():
            if contestant not in names:
                del counter[contestant]

        self.config['lastvoice'] = currentvoice
        self.config['currentvoice'] = None

        entries = []
        numspeakers = 0
        total = 0
        for speaker, count in counter.iteritems():
            numspeakers += 1
            total += count
            entries.extend([speaker] * count)

        if not counter:
            say("I was going to do the voice of the day, but nobody seems to be eligible =(")
            self.config.save()
            return

        say("Ready everyone? It's time to choose a new Voice of the Day!")
        yield delay(3)

        lastdrawing = self.config['lastdrawing']
        #if lastdrawing:
        #    say("There have been {0} speakers, totaling {1} lines of chat since the last drawing {2}".format(
        #        numspeakers,
        #        total,
        #        prettydate(
        #            datetime.datetime.fromtimestamp(lastdrawing)
        #            ),
        #        ))
        #    yield delay(2)
        self.config['lastdrawing'] = time.time()

        winner = random.choice(entries)
        self.config['currentvoice'] = winner
        self.config.save()
        say("...aaaaand the winner is....")
        yield delay(1)
        say("{0}!".format(winner))

        yield delay(1)
        yield self._send_as_op(Event("irc.do_mode",
            channel=channel,
            set=True,
            modes="v",
            user=winner,
            ))
        yield delay(5)
        say("until next time...")

        self._set_timer()
        

            
    def draw(self, event, match):
        channel = event.channel
        if self.config['channel'] and self.config['channel'] != channel:
            event.reply("I can only do that in {0}".format(self.config['channel']))
        else:
            if self.timer:
                self.timer.cancel()
                self.timer = None
            self._do_votd(channel)

    @defer.inlineCallbacks
    def on_event_irc_on_privmsg(self, event):
        super(VoiceOfTheDay, self).on_event_irc_on_privmsg(event)

        self.lastspoken = time.time()

        # This delay is a bit of a hack. If we do e.g. a configreload, and this
        # handler happens to run before the reload, this will save the config,
        # clobbering the new one. So here we wait a second to let the other
        # handler run, reload our config, THEN we increment the counter.
        yield delay(1)
        if event.channel == self.config["channel"]:
            nick = event.user.split("!")[0]
            self.config["counter"][nick] += 1
            self.config.save()