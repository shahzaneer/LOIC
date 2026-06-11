from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable, Optional
from urllib.parse import unquote

logger = logging.getLogger(__name__)


class HiveMindClient:
    def __init__(
        self,
        server: str,
        port: int,
        channel: str,
        on_params: Callable[[list[str]], None],
    ):
        self.server = server
        self.port = port
        self.channel = channel if channel.startswith("#") else f"#{channel}"
        self._on_params = on_params
        self._op_list: dict[str, str] = {}
        self._enabled = False
        self._bot = None
        self._thread: Optional[threading.Thread] = None
        self._reconnect_attempts = 0
        self._max_reconnects = 10

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self):
        try:
            import irc.bot
            import irc.strings
        except ImportError:
            logger.error("IRC library not installed. Install with: pip install irc")
            return

        if self._enabled:
            return
        self._enabled = True
        self._reconnect_attempts = 0

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("HiveMind connecting to %s:%d %s", self.server, self.port, self.channel)

    def stop(self):
        self._enabled = False
        if self._bot:
            try:
                self._bot.disconnect("HiveMind disconnecting")
            except Exception:
                pass

    def _run(self):
        import irc.bot

        while self._enabled and self._reconnect_attempts < self._max_reconnects:
            try:
                from loic.functions import random_string
                nickname = f"LOIC_{random_string()}"
                self._bot = irc.bot.SingleServerIRCBot(
                    [(self.server, self.port)],
                    nickname,
                    "Python LOIC HiveMind",
                )
                self._bot.connection.add_global_handler("welcome", self._on_welcome)
                self._bot.connection.add_global_handler("namreply", self._on_names)
                self._bot.connection.add_global_handler("op", self._on_op)
                self._bot.connection.add_global_handler("deop", self._on_deop)
                self._bot.connection.add_global_handler("part", self._on_part)
                self._bot.connection.add_global_handler("quit", self._on_quit)
                self._bot.connection.add_global_handler("kick", self._on_kick)
                self._bot.connection.add_global_handler("nick", self._on_nick_change)
                self._bot.connection.add_global_handler("topic", self._on_topic)
                self._bot.connection.add_global_handler("pubmsg", self._on_pubmsg)
                self._bot.connection.add_global_handler("disconnect", self._on_disconnect)

                self._bot.start()
                self._reconnect_attempts = 0

            except Exception as e:
                self._reconnect_attempts += 1
                logger.warning("IRC connection error (attempt %d/%d): %s", self._reconnect_attempts, self._max_reconnects, e)
                if self._enabled:
                    import time
                    time.sleep(min(30, 2 ** self._reconnect_attempts))

        logger.info("HiveMind client stopped")

    def _on_welcome(self, connection, event):
        connection.join(self.channel)
        self._reconnect_attempts = 0
        logger.info("Joined %s", self.channel)

    def _on_names(self, connection, event):
        new_ops = {}
        nicknames = event.arguments[0].split() if event.arguments else []
        for nick in nicknames:
            if nick.startswith(("@", "&", "~", "+")):
                clean = nick.lstrip("@&~+")
                new_ops[clean] = ""
        self._op_list.update(new_ops)

    def _on_op(self, connection, event):
        nick = event.arguments[0] if event.arguments else ""
        if nick and nick not in self._op_list:
            self._op_list[nick] = ""

    def _on_deop(self, connection, event):
        nick = event.arguments[0] if event.arguments else ""
        self._op_list.pop(nick, None)

    def _on_part(self, connection, event):
        nick = getattr(event.source, "nick", "") if event.source else ""
        self._op_list.pop(nick, None)

    def _on_quit(self, connection, event):
        nick = getattr(event.source, "nick", "") if event.source else ""
        self._op_list.pop(nick, None)

    def _on_kick(self, connection, event):
        nick = event.arguments[0] if event.arguments else ""
        self._op_list.pop(nick, None)

    def _on_nick_change(self, connection, event):
        old = getattr(event.source, "nick", "") if event.source else ""
        new = event.target if event.target else ""
        if old in self._op_list:
            self._op_list.pop(old, None)
            if new not in self._op_list:
                self._op_list[new] = ""

    def _on_topic(self, connection, event):
        topic = event.arguments[0] if event.arguments else ""
        if topic.lower().startswith("!lazor "):
            pars = topic.split()
            logger.info("IRC topic command: %s", topic[:80])
            try:
                self._on_params(pars)
            except Exception as e:
                logger.error("Error processing IRC topic command: %s", e)

    def _on_pubmsg(self, connection, event):
        msg = event.arguments[0] if event.arguments else ""
        nick = getattr(event.source, "nick", "") if event.source else ""
        if msg.lower().startswith("!lazor ") and nick in self._op_list:
            pars = msg.split()
            logger.info("IRC command from %s: %s", nick, msg[:80])
            try:
                self._on_params(pars)
            except Exception as e:
                logger.error("Error processing IRC command: %s", e)

    def _on_disconnect(self, connection, event):
        if self._enabled:
            self._reconnect_attempts += 1
            logger.info("IRC disconnected, will reconnect (attempt %d)", self._reconnect_attempts)


def parse_irc_params(pars: list[str]) -> dict:
    result: dict = {}
    for param in pars:
        if "=" in param:
            key, _, val = param.partition("=")
            result[key.lower()] = val
        else:
            result[param.lower()] = True
    return result