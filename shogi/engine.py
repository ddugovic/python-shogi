# This file is part of the python-chess library.
# Copyright (C) 2012-2020 Niklas Fiekas <niklas.fiekas@backscattering.de>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

import abc
import asyncio
import collections
import concurrent.futures
import contextlib
import copy
import enum
import logging
import warnings
import shlex
import subprocess
import sys
import threading
import typing
import os
import re

from types import TracebackType
from typing import Any, Awaitable, Callable, Coroutine, Deque, Dict, Generator, Generic, Iterable, Iterator, List, Mapping, MutableMapping, NamedTuple, Optional, Text, Tuple, Type, TypeVar, Union

try:
    # Python 3.7
    from asyncio import get_running_loop as _get_running_loop
except ImportError:
    from asyncio import _get_running_loop

try:
    # Python 3.7
    from asyncio import all_tasks as _all_tasks
except ImportError:
    _all_tasks = asyncio.Task.all_tasks

try:
    # Python 3.7
    from asyncio import run as _run
except ImportError:
    _T = TypeVar("_T")

    def _run(main: Awaitable[_T], *, debug: bool = False) -> _T:
        assert _get_running_loop() is None
        assert asyncio.iscoroutine(main)

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.set_debug(debug)
            return loop.run_until_complete(main)
        finally:
            try:
                loop.run_until_complete(asyncio.gather(*_all_tasks(loop), return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                asyncio.set_event_loop(None)
                loop.close()

import shogi


T = TypeVar("T")
EngineProtocolT = TypeVar("EngineProtocolT", bound="EngineProtocol")

ConfigValue = Union[str, int, bool, None]
ConfigMapping = Mapping[str, ConfigValue]


LOGGER = logging.getLogger(__name__)


MANAGED_OPTIONS = ["multipv", "ponder"]


class EventLoopPolicy(asyncio.AbstractEventLoopPolicy):
    """
    An event loop policy for thread-local event loops and child watchers.
    Ensures each event loop is capable of spawning and watching subprocesses,
    even when not running on the main thread.

    Windows: Uses :class:`~asyncio.ProactorEventLoop`.

    Unix: Uses :class:`~asyncio.SelectorEventLoop`. If available,
    :class:`~asyncio.PidfdChildWatcher` is used to detect subprocess
    termination (Python 3.9+ on Linux 5.3+). Otherwise the default child
    watcher is used on the main thread and relatively slow eager polling
    is used on all other threads.
    """
    class _Local(threading.local):
        loop: Optional[asyncio.AbstractEventLoop] = None
        set_called = False
        watcher: "Optional[asyncio.AbstractChildWatcher]" = None

    def __init__(self) -> None:
        self._local = self._Local()

    def get_event_loop(self):
        if self._local.loop is None and not self._local.set_called and threading.current_thread() is threading.main_thread():
            self.set_event_loop(self.new_event_loop())
        if self._local.loop is None:
            raise RuntimeError(f"no current event loop in thread {threading.current_thread().name!r}")
        return self._local.loop

    def set_event_loop(self, loop):
        assert loop is None or isinstance(loop, asyncio.AbstractEventLoop)
        self._local.set_called = True
        self._local.loop = loop
        if self._local.watcher is not None:
            self._local.watcher.attach_loop(loop)

    def new_event_loop(self):
        return asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.SelectorEventLoop()

    def get_child_watcher(self):
        if self._local.watcher is None:
            self._local.watcher = self._init_watcher()
            self._local.watcher.attach_loop(self._local.loop)
        return self._local.watcher

    def set_child_watcher(self, watcher):
        assert watcher is None or isinstance(watcher, asyncio.AbstractChildWatcher)
        if self._local.watcher is not None:
            self._local.watcher.close()
        self._local.watcher = watcher

    def _init_watcher(self):
        if sys.platform == "win32":
            raise NotImplementedError

        try:
            os.close(os.pidfd_open(os.getpid()))
            return asyncio.PidfdChildWatcher()
        except (AttributeError, OSError):
            # Before Python 3.9 or before Linux 5.3 or the syscall is not
            # permitted.
            pass

        if threading.current_thread() is threading.main_thread():
            try:
                return asyncio.ThreadedChildWatcher()
            except AttributeError:
                # Before Python 3.8.
                return asyncio.SafeChildWatcher()

        class PollingChildWatcher(asyncio.SafeChildWatcher):
            _loop: Optional[asyncio.AbstractEventLoop]
            _callbacks: Dict[int, Any]

            def __init__(self) -> None:
                super().__init__()
                self._poll_handle: Optional[asyncio.Handle] = None
                self._poll_delay = 0.001

            def attach_loop(self, loop: Optional[asyncio.AbstractEventLoop]) -> None:
                assert loop is None or isinstance(loop, asyncio.AbstractEventLoop)

                if self._loop is not None and loop is None and self._callbacks:
                    warnings.warn("A loop is being detached from a child watcher with pending handlers", RuntimeWarning)

                if self._poll_handle is not None:
                    self._poll_handle.cancel()

                self._loop = loop
                if self._loop is not None:
                    self._poll_handle = self._loop.call_soon(self._poll)
                    self._do_waitpid_all()  # type: ignore

            def _poll(self) -> None:
                if self._loop:
                    self._do_waitpid_all()  # type: ignore
                    self._poll_delay = min(self._poll_delay * 2, 1.0)
                    self._poll_handle = self._loop.call_later(self._poll_delay, self._poll)

        return PollingChildWatcher()


def run_in_background(coroutine: "Callable[[concurrent.futures.Future[T]], Coroutine[Any, Any, None]]", *, name: Optional[str] = None, debug: bool = False, _policy_lock: threading.Lock = threading.Lock()) -> T:
    """
    Runs ``coroutine(future)`` in a new event loop on a background thread.

    Blocks and returns the *future* result as soon as it is resolved.
    The coroutine and all remaining tasks continue running in the background
    until it is complete.

    Note: This installs a :class:`shogi.engine.EventLoopPolicy` for the entire
    process.
    """
    assert asyncio.iscoroutinefunction(coroutine)

    with _policy_lock:
        if not isinstance(asyncio.get_event_loop_policy(), EventLoopPolicy):
            asyncio.set_event_loop_policy(EventLoopPolicy())

    future: concurrent.futures.Future[T] = concurrent.futures.Future()

    def background() -> None:
        try:
            _run(coroutine(future))
            future.cancel()
        except Exception as exc:
            future.set_exception(exc)

    threading.Thread(target=background, name=name).start()
    return future.result()


class EngineError(RuntimeError):
    """Runtime error caused by a misbehaving engine or incorrect usage."""


class EngineTerminatedError(EngineError):
    """The engine process exited unexpectedly."""


class AnalysisComplete(Exception):
    """
    Raised when analysis is complete, all information has been consumed, but
    further information was requested.
    """


class Option(NamedTuple):
    """Information about an available engine option."""

    name: str
    type: str
    default: ConfigValue
    min: Optional[int]
    max: Optional[int]
    var: Optional[List[str]]

    def parse(self, value: ConfigValue) -> ConfigValue:
        if self.type == "check":
            return value and value != "false"
        elif self.type == "spin":
            try:
                value = int(value)  # type: ignore
            except ValueError:
                raise EngineError(f"expected integer for spin option {self.name!r}, got: {value!r}")
            if self.min is not None and value < self.min:
                raise EngineError(f"expected value for option {self.name!r} to be at least {self.min}, got: {value}")
            if self.max is not None and self.max < value:
                raise EngineError(f"expected value for option {self.name!r} to be at most {self.max}, got: {value}")
            return value
        elif self.type == "combo":
            value = str(value)
            if value not in (self.var or []):
                raise EngineError("invalid value for combo option {!r}, got: {} (available: {})".format(self.name, value, ", ".join(self.var) if self.var else "-"))
            return value
        elif self.type in ["button", "reset", "save"]:
            return None
        elif self.type in ["string", "file", "path"]:
            value = str(value)
            if "\n" in value or "\r" in value:
                raise EngineError(f"invalid line-break in string option {self.name!r}: {value!r}")
            return value
        else:
            raise EngineError(f"unknown option type: {self.type!r}")

    def is_managed(self) -> bool:
        """
        Some options are managed automatically: ``MultiPV``, ``Ponder``.
        """
        return self.name.lower() in MANAGED_OPTIONS


class Limit:
    """Search-termination condition."""

    def __init__(self, *,
                 time: Optional[float] = None,
                 depth: Optional[int] = None,
                 nodes: Optional[int] = None,
                 mate: Optional[int] = None,
                 white_clock: Optional[float] = None,
                 black_clock: Optional[float] = None,
                 white_inc: Optional[float] = None,
                 black_inc: Optional[float] = None,
                 remaining_moves: Optional[int] = None):
        self.time = time
        self.depth = depth
        self.nodes = nodes
        self.mate = mate
        self.white_clock = white_clock
        self.black_clock = black_clock
        self.white_inc = white_inc
        self.black_inc = black_inc
        self.remaining_moves = remaining_moves

    def __repr__(self) -> str:
        return "{}({})".format(
            type(self).__name__,
            ", ".join("{}={!r}".format(attr, getattr(self, attr))
                      for attr in ["time", "depth", "nodes", "mate", "white_clock", "black_clock", "white_inc", "black_inc", "remaining_moves"]
                      if getattr(self, attr) is not None))


try:
    class InfoDict(typing.TypedDict, total=False):
        """Dictionary of extra information sent by the engine."""
        score: "PovScore"
        pv: List[shogi.Move]
        depth: int
        seldepth: int
        time: float
        nodes: int
        nps: int
        tbhits: int
        multipv: int
        currmove: shogi.Move
        currmovenumber: int
        hashfull: int
        cpuload: int
        refutation: Dict[shogi.Move, List[shogi.Move]]
        currline: Dict[int, List[shogi.Move]]
        ebf: float
        wdl: Tuple[int, int, int]
        string: str
except AttributeError:
    # Before Python 3.8.
    InfoDict = dict  # type: ignore


class PlayResult:
    """Returned by :func:`shogi.engine.EngineProtocol.play()`."""

    def __init__(self,
                 move: Optional[shogi.Move],
                 ponder: Optional[shogi.Move],
                 info: Optional[InfoDict] = None,
                 *,
                 draw_offered: bool = False,
                 resigned: bool = False) -> None:
        self.move = move
        self.ponder = ponder
        self.info: InfoDict = info or {}
        self.draw_offered = draw_offered
        self.resigned = resigned

    def __repr__(self) -> str:
        return "<{} at {:#x} (move={}, ponder={}, info={}, draw_offered={}, resigned={})>".format(
            type(self).__name__, id(self), self.move, self.ponder, self.info,
            self.draw_offered, self.resigned)


class Info(enum.IntFlag):
    """Used to filter information sent by the shogi engine."""
    NONE = 0
    BASIC = 1
    SCORE = 2
    PV = 4
    REFUTATION = 8
    CURRLINE = 16
    ALL = BASIC | SCORE | PV | REFUTATION | CURRLINE

INFO_NONE = Info.NONE
INFO_BASIC = Info.BASIC
INFO_SCORE = Info.SCORE
INFO_PV = Info.PV
INFO_REFUTATION = Info.REFUTATION
INFO_CURRLINE = Info.CURRLINE
INFO_ALL = Info.ALL


class PovScore:
    """A relative :class:`~shogi.engine.Score` and the point of view."""

    def __init__(self, relative: "Score", turn: shogi.Color) -> None:
        self.relative = relative
        self.turn = turn

    def white(self) -> "Score":
        """Gets the score from White's point of view."""
        return self.pov(shogi.WHITE)

    def black(self) -> "Score":
        """Gets the score from Black's point of view."""
        return self.pov(shogi.BLACK)

    def pov(self, color: shogi.Color) -> "Score":
        """Gets the score from the point of view of the given *color*."""
        return self.relative if self.turn == color else -self.relative

    def is_mate(self) -> bool:
        """Tests if this is a mate score."""
        return self.relative.is_mate()

    def __repr__(self) -> str:
        return "PovScore({!r}, {})".format(self.relative, "WHITE" if self.turn else "BLACK")

    def __str__(self) -> str:
        return str(self.relative)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, PovScore):
            return self.white() == other.white()
        else:
            return NotImplemented


class Score(abc.ABC):
    """
    Evaluation of a position.

    The score can be :class:`~shogi.engine.Cp` (centi-pawns),
    :class:`~shogi.engine.Mate` or :py:data:`~shogi.engine.MateGiven`.
    A positive value indicates an advantage.

    There is a total order defined on centi-pawn and mate scores.

    >>> from shogi.engine import Cp, Mate, MateGiven
    >>>
    >>> Mate(-0) < Mate(-1) < Cp(-50) < Cp(200) < Mate(4) < Mate(1) < MateGiven
    True

    Scores can be negated to change the point of view:

    >>> -Cp(20)
    Cp(-20)

    >>> -Mate(-4)
    Mate(+4)

    >>> -Mate(0)
    MateGiven
    """

    @abc.abstractmethod
    def score(self, *, mate_score: Optional[int] = None) -> Optional[int]:
        """
        Returns the centi-pawn score as an integer or ``None``.

        You can optionally pass a large value to convert mate scores to
        centi-pawn scores.

        >>> Cp(-300).score()
        -300
        >>> Mate(5).score() is None
        True
        >>> Mate(5).score(mate_score=100000)
        99995
        """

    @abc.abstractmethod
    def mate(self) -> Optional[int]:
        """
        Returns the number of plies to mate, negative if we are getting
        mated, or ``None``.

        .. warning::
            This conflates ``Mate(0)`` (we lost) and ``MateGiven``
            (we won) to ``0``.
        """

    def is_mate(self) -> bool:
        """Tests if this is a mate score."""
        return self.mate() is not None

    @abc.abstractmethod
    def __neg__(self) -> "Score":
        pass

    def _score_tuple(self) -> Tuple[bool, bool, bool, int, Optional[int]]:
        mate = self.mate()
        return (
            isinstance(self, MateGivenType),
            mate is not None and mate > 0,
            mate is None,
            -(mate or 0),
            self.score(),
        )

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Score):
            return self._score_tuple() == other._score_tuple()
        else:
            return NotImplemented

    def __lt__(self, other: object) -> bool:
        if isinstance(other, Score):
            return self._score_tuple() < other._score_tuple()
        else:
            return NotImplemented

    def __le__(self, other: object) -> bool:
        if isinstance(other, Score):
            return self._score_tuple() <= other._score_tuple()
        else:
            return NotImplemented

    def __gt__(self, other: object) -> bool:
        if isinstance(other, Score):
            return self._score_tuple() > other._score_tuple()
        else:
            return NotImplemented

    def __ge__(self, other: object) -> bool:
        if isinstance(other, Score):
            return self._score_tuple() >= other._score_tuple()
        else:
            return NotImplemented


class Cp(Score):
    """Centi-pawn score."""

    def __init__(self, cp: int) -> None:
        self.cp = cp

    def mate(self) -> None:
        return None

    def score(self, *, mate_score: Optional[int] = None) -> int:
        return self.cp

    def __str__(self) -> str:
        return f"+{self.cp:d}" if self.cp > 0 else str(self.cp)

    def __repr__(self) -> str:
        return f"Cp({self})"

    def __neg__(self) -> "Cp":
        return Cp(-self.cp)

    def __pos__(self) -> "Cp":
        return Cp(self.cp)

    def __abs__(self) -> "Cp":
        return Cp(abs(self.cp))


class Mate(Score):
    """Mate score."""

    def __init__(self, moves: int) -> None:
        self.moves = moves

    def mate(self) -> int:
        return self.moves

    def score(self, *, mate_score: Optional[int] = None) -> Optional[int]:
        if mate_score is None:
            return None
        elif self.moves > 0:
            return mate_score - self.moves
        else:
            return -mate_score - self.moves

    def __str__(self) -> str:
        return f"#+{self.moves}" if self.moves > 0 else f"#-{abs(self.moves)}"

    def __repr__(self) -> str:
        return "Mate({})".format(str(self).lstrip("#"))

    def __neg__(self) -> Union["MateGivenType", "Mate"]:
        return MateGiven if not self.moves else Mate(-self.moves)

    def __pos__(self) -> "Mate":
        return Mate(self.moves)

    def __abs__(self) -> Union["MateGivenType", "Mate"]:
        return MateGiven if not self.moves else Mate(abs(self.moves))


class MateGivenType(Score):
    """Winning mate score, equivalent to ``-Mate(0)``."""

    def mate(self) -> int:
        return 0

    def score(self, *, mate_score: Optional[int] = None) -> Optional[int]:
        return mate_score

    def __neg__(self) -> Mate:
        return Mate(0)

    def __pos__(self) -> "MateGivenType":
        return self

    def __abs__(self) -> "MateGivenType":
        return self

    def __repr__(self) -> str:
        return "MateGiven"

    def __str__(self) -> str:
        return "#+0"

MateGiven = MateGivenType()


class MockTransport(asyncio.SubprocessTransport):
    def __init__(self, protocol: "EngineProtocol") -> None:
        super().__init__()
        self.protocol = protocol
        self.expectations: Deque[Tuple[str, List[str]]] = collections.deque()
        self.expected_pings = 0
        self.stdin_buffer = bytearray()
        self.protocol.connection_made(self)

    def expect(self, expectation: str, responses: List[str] = []) -> None:
        self.expectations.append((expectation, responses))

    def expect_ping(self) -> None:
        self.expected_pings += 1

    def assert_done(self) -> None:
        assert not self.expectations, f"pending expectations: {self.expectations}"

    def get_pipe_transport(self, fd: int) -> "MockTransport":  # type: ignore
        assert fd == 0, f"expected 0 for stdin, got {fd}"
        return self

    def write(self, data: bytes) -> None:
        self.stdin_buffer.extend(data)
        while b"\n" in self.stdin_buffer:
            line_bytes, self.stdin_buffer = self.stdin_buffer.split(b"\n", 1)
            line = line_bytes.decode("utf-8")

            if line.startswith("ping ") and self.expected_pings:
                self.expected_pings -= 1
                self.protocol.pipe_data_received(1, line.replace("ping ", "pong ").encode("utf-8") + b"\n")
            else:
                assert self.expectations, f"unexpected: {line}"
                expectation, responses = self.expectations.popleft()
                assert expectation == line, f"expected {expectation}, got: {line}"
                if responses:
                    self.protocol.pipe_data_received(1, "\n".join(responses).encode("utf-8") + b"\n")

    def get_pid(self) -> int:
        return id(self)

    def get_returncode(self) -> Optional[int]:
        return None if self.expectations else 0


class EngineProtocol(asyncio.SubprocessProtocol, metaclass=abc.ABCMeta):
    """Protocol for communicating with a shogi engine process."""

    id: Dict[str, str]
    options: MutableMapping[str, Option]

    def __init__(self: EngineProtocolT) -> None:
        self.loop = _get_running_loop()
        self.transport: Optional[asyncio.SubprocessTransport] = None

        self.buffer = {
            1: bytearray(),  # stdout
            2: bytearray(),  # stderr
        }

        self.command: Optional[BaseCommand[EngineProtocolT, Any]] = None
        self.next_command: Optional[BaseCommand[EngineProtocolT, Any]] = None

        self.initialized = False
        self.returncode: asyncio.Future[int] = asyncio.Future()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        # SubprocessTransport expected, but not checked to allow duck typing.
        self.transport = transport  # type: ignore
        LOGGER.debug("%s: Connection made", self)

    def connection_lost(self: EngineProtocolT, exc: Optional[Exception]) -> None:
        assert self.transport is not None
        code = self.transport.get_returncode()
        assert code is not None, "connect lost, but got no returncode"
        LOGGER.debug("%s: Connection lost (exit code: %d, error: %s)", self, code, exc)

        # Terminate commands.
        if self.command is not None:
            self.command._engine_terminated(self, code)
            self.command = None
        if self.next_command is not None:
            self.next_command._engine_terminated(self, code)
            self.next_command = None

        self.returncode.set_result(code)

    def process_exited(self) -> None:
        LOGGER.debug("%s: Process exited", self)

    def send_line(self, line: str) -> None:
        LOGGER.debug("%s: << %s", self, line)
        assert self.transport is not None, "cannot send line before connection is made"
        stdin = self.transport.get_pipe_transport(0)
        assert stdin is not None, "no pipe for stdin"
        stdin.write(line.encode("utf-8"))
        stdin.write(b"\n")

    def pipe_data_received(self, fd: int, data: Union[bytes, str]) -> None:
        self.buffer[fd].extend(data)  # type: ignore
        while b"\n" in self.buffer[fd]:
            line_bytes, self.buffer[fd] = self.buffer[fd].split(b"\n", 1)
            if line_bytes.endswith(b"\r"):
                line_bytes = line_bytes[:-1]
            line = line_bytes.decode("utf-8")
            if fd == 1:
                self.loop.call_soon(self._line_received, line)
            else:
                self.loop.call_soon(self.error_line_received, line)

    def error_line_received(self, line: str) -> None:
        LOGGER.warning("%s: stderr >> %s", self, line)

    def _line_received(self: EngineProtocolT, line: str) -> None:
        LOGGER.debug("%s: >> %s", self, line)

        self.line_received(line)

        if self.command:
            self.command._line_received(self, line)

    def line_received(self, line: str) -> None:
        pass

    async def communicate(self: EngineProtocolT, command_factory: Callable[[], "BaseCommand[EngineProtocolT, T]"]) -> T:
        command = command_factory()

        if self.returncode.done():
            raise EngineTerminatedError(f"engine process dead (exit code: {self.returncode.result()})")

        assert command.state == CommandState.New

        if self.next_command is not None:
            self.next_command.result.cancel()
            self.next_command.finished.cancel()
            self.next_command._done()

        self.next_command = command

        def previous_command_finished(_: "Optional[asyncio.Future[None]]") -> None:
            if self.command is not None:
                self.command._done()

            self.command, self.next_command = self.next_command, None
            if self.command is not None:
                cmd = self.command

                def cancel_if_cancelled(result):
                    if result.cancelled():
                        cmd._cancel(self)

                cmd.result.add_done_callback(cancel_if_cancelled)
                cmd.finished.add_done_callback(previous_command_finished)
                cmd._start(self)

        if self.command is None:
            previous_command_finished(None)
        elif not self.command.result.done():
            self.command.result.cancel()
        elif not self.command.result.cancelled():
            self.command._cancel(self)

        return await command.result

    def __repr__(self) -> str:
        pid = self.transport.get_pid() if self.transport is not None else "?"
        return f"<{type(self).__name__} (pid={pid})>"

    @abc.abstractmethod
    async def initialize(self) -> None:
        """Initializes the engine."""

    @abc.abstractmethod
    async def ping(self) -> None:
        """
        Pings the engine and waits for a response. Used to ensure the engine
        is still alive and idle.
        """

    @abc.abstractmethod
    async def configure(self, options: ConfigMapping) -> None:
        """
        Configures global engine options.

        :param options: A dictionary of engine options where the keys are
            names of :py:attr:`~options`. Do not set options that are
            managed automatically (:func:`shogi.engine.Option.is_managed()`).
        """

    @abc.abstractmethod
    async def play(self, board: shogi.Board, limit: Limit, *, game: object = None, info: Info = INFO_NONE, ponder: bool = False, root_moves: Optional[Iterable[shogi.Move]] = None, options: ConfigMapping = {}) -> PlayResult:
        """
        Plays a position.

        :param board: The position. The entire move stack will be sent to the
            engine.
        :param limit: An instance of :class:`shogi.engine.Limit` that
            determines when to stop thinking.
        :param game: Optional. An arbitrary object that identifies the game.
            Will automatically inform the engine if the object is not equal
            to the previous game (e.g., ``usinewgame``, ``new``).
        :param info: Selects which additional information to retrieve from the
            engine. ``INFO_NONE``, ``INFO_BASE`` (basic information that is
            trivial to obtain), ``INFO_SCORE``, ``INFO_PV``,
            ``INFO_REFUTATION``, ``INFO_CURRLINE``, ``INFO_ALL`` or any
            bitwise combination. Some overhead is associated with parsing
            extra information.
        :param ponder: Whether the engine should keep analysing in the
            background even after the result has been returned.
        :param root_moves: Optional. Consider only root moves from this list.
        :param options: Optional. A dictionary of engine options for the
            analysis. The previous configuration will be restored after the
            analysis is complete. You can permanently apply a configuration
            with :func:`~shogi.engine.EngineProtocol.configure()`.
        """

    @typing.overload
    async def analyse(self, board: shogi.Board, limit: Limit, *, game: object = None, info: Info = INFO_ALL, root_moves: Optional[Iterable[shogi.Move]] = None, options: ConfigMapping = {}) -> InfoDict: ...
    @typing.overload
    async def analyse(self, board: shogi.Board, limit: Limit, *, multipv: int, game: object = None, info: Info = INFO_ALL, root_moves: Optional[Iterable[shogi.Move]] = None, options: ConfigMapping = {}) -> InfoDict: ...
    @typing.overload
    async def analyse(self, board: shogi.Board, limit: Limit, *, multipv: Optional[int] = None, game: object = None, info: Info = INFO_ALL, root_moves: Optional[Iterable[shogi.Move]] = None, options: ConfigMapping = {}) -> Union[List[InfoDict], InfoDict]: ...
    async def analyse(self, board: shogi.Board, limit: Limit, *, multipv: Optional[int] = None, game: object = None, info: Info = INFO_ALL, root_moves: Optional[Iterable[shogi.Move]] = None, options: ConfigMapping = {}) -> Union[List[InfoDict], InfoDict]:
        """
        Analyses a position and returns a dictionary of
        `information <#shogi.engine.PlayResult.info>`_.

        :param board: The position to analyse. The entire move stack will be
            sent to the engine.
        :param limit: An instance of :class:`shogi.engine.Limit` that
            determines when to stop the analysis.
        :param multipv: Optional. Analyse multiple root moves. Will return
            a list of at most *multipv* dictionaries rather than just a single
            info dictionary.
        :param game: Optional. An arbitrary object that identifies the game.
            Will automatically inform the engine if the object is not equal
            to the previous game (e.g., ``usinewgame``, ``new``).
        :param info: Selects which information to retrieve from the
            engine. ``INFO_NONE``, ``INFO_BASE`` (basic information that is
            trivial to obtain), ``INFO_SCORE``, ``INFO_PV``,
            ``INFO_REFUTATION``, ``INFO_CURRLINE``, ``INFO_ALL`` or any
            bitwise combination. Some overhead is associated with parsing
            extra information.
        :param root_moves: Optional. Limit analysis to a list of root moves.
        :param options: Optional. A dictionary of engine options for the
            analysis. The previous configuration will be restored after the
            analysis is complete. You can permanently apply a configuration
            with :func:`~shogi.engine.EngineProtocol.configure()`.
        """
        analysis = await self.analysis(board, limit, multipv=multipv, game=game, info=info, root_moves=root_moves, options=options)

        with analysis:
            await analysis.wait()

        return analysis.info if multipv is None else analysis.multipv

    @abc.abstractmethod
    async def analysis(self, board: shogi.Board, limit: Optional[Limit] = None, *, multipv: Optional[int] = None, game: object = None, info: Info = INFO_ALL, root_moves: Optional[Iterable[shogi.Move]] = None, options: ConfigMapping = {}) -> "AnalysisResult":
        """
        Starts analysing a position.

        :param board: The position to analyse. The entire move stack will be
            sent to the engine.
        :param limit: Optional. An instance of :class:`shogi.engine.Limit`
            that determines when to stop the analysis. Analysis is infinite
            by default.
        :param multipv: Optional. Analyse multiple root moves.
        :param game: Optional. An arbitrary object that identifies the game.
            Will automatically inform the engine if the object is not equal
            to the previous game (e.g., ``usinewgame``, ``new``).
        :param info: Selects which information to retrieve from the
            engine. ``INFO_NONE``, ``INFO_BASE`` (basic information that is
            trivial to obtain), ``INFO_SCORE``, ``INFO_PV``,
            ``INFO_REFUTATION``, ``INFO_CURRLINE``, ``INFO_ALL`` or any
            bitwise combination. Some overhead is associated with parsing
            extra information.
        :param root_moves: Optional. Limit analysis to a list of root moves.
        :param options: Optional. A dictionary of engine options for the
            analysis. The previous configuration will be restored after the
            analysis is complete. You can permanently apply a configuration
            with :func:`~shogi.engine.EngineProtocol.configure()`.

        Returns :class:`~shogi.engine.AnalysisResult`, a handle that allows
        asynchronously iterating over the information sent by the engine
        and stopping the analysis at any time.
        """

    @abc.abstractmethod
    async def quit(self) -> None:
        """Asks the engine to shut down."""

    @classmethod
    async def popen(cls: Type[EngineProtocolT], command: Union[str, List[str]], *, setpgrp: bool = False, **kwargs: Any) -> Tuple[asyncio.SubprocessTransport, EngineProtocolT]:
        if not isinstance(command, list):
            command = [command]

        popen_args = {}
        if setpgrp:
            try:
                # Windows.
                popen_args["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore
            except AttributeError:
                # Unix.
                popen_args["preexec_fn"] = os.setpgrp
        popen_args.update(kwargs)

        return await _get_running_loop().subprocess_exec(cls, *command, **popen_args)  # type: ignore


class CommandState(enum.Enum):
    New = 1
    Active = 2
    Cancelling = 3
    Done = 4


class BaseCommand(Generic[EngineProtocolT, T]):
    def __init__(self) -> None:
        self.state = CommandState.New

        self.result: asyncio.Future[T] = asyncio.Future()
        self.finished: asyncio.Future[None] = asyncio.Future()

    def _engine_terminated(self, engine: EngineProtocolT, code: int) -> None:
        exc = EngineTerminatedError(f"engine process died unexpectedly (exit code: {code})")
        if self.state == CommandState.Active:
            self.engine_terminated(engine, exc)
        elif self.state == CommandState.Cancelling:
            self.finished.set_result(None)
        elif self.state == CommandState.New:
            self._handle_exception(engine, exc)

    def _handle_exception(self, engine: EngineProtocolT, exc: Exception) -> None:
        if not self.result.done():
            self.result.set_exception(exc)
        else:
            engine.loop.call_exception_handler({
                "message": f"{type(self).__name__} failed after returning preliminary result ({self.result!r})",
                "exception": exc,
                "protocol": engine,
                "transport": engine.transport,
            })

        if not self.finished.done():
            self.finished.set_result(None)

    def set_finished(self) -> None:
        assert self.state in [CommandState.Active, CommandState.Cancelling]
        if not self.result.done():
            self.result.set_exception(EngineError(f"engine command finished before returning result: {self!r}"))
        self.finished.set_result(None)

    def _cancel(self, engine: EngineProtocolT) -> None:
        assert self.state == CommandState.Active
        self.state = CommandState.Cancelling
        self.cancel(engine)

    def _start(self, engine: EngineProtocolT) -> None:
        assert self.state == CommandState.New
        self.state = CommandState.Active
        try:
            self.check_initialized(engine)
            self.start(engine)
        except EngineError as err:
            self._handle_exception(engine, err)

    def _done(self) -> None:
        assert self.state != CommandState.Done
        self.state = CommandState.Done

    def _line_received(self, engine: EngineProtocolT, line: str) -> None:
        assert self.state in [CommandState.Active, CommandState.Cancelling]
        try:
            self.line_received(engine, line)
        except EngineError as err:
            self._handle_exception(engine, err)

    def cancel(self, engine: EngineProtocolT) -> None:
        pass

    def check_initialized(self, engine: EngineProtocolT) -> None:
        if not engine.initialized:
            raise EngineError("tried to run command, but engine is not initialized")

    def start(self, engine: EngineProtocolT) -> None:
        raise NotImplementedError

    def line_received(self, engine: EngineProtocolT, line: str) -> None:
        pass

    def engine_terminated(self, engine: EngineProtocolT, exc: Exception) -> None:
        self._handle_exception(engine, exc)

    def __repr__(self) -> str:
        return "<{} at {:#x} (state={}, result={}, finished={}>".format(type(self).__name__, id(self), self.state, self.result, self.finished)


class UciProtocol(EngineProtocol):
    """
    An implementation of the
    `Universal Chess Interface <https://www.shogiprogramming.org/USI>`_
    protocol.
    """

    def __init__(self) -> None:
        super().__init__()
        self.options: UciOptionMap[Option] = UciOptionMap()
        self.config: UciOptionMap[ConfigValue] = UciOptionMap()
        self.target_config: UciOptionMap[ConfigValue] = UciOptionMap()
        self.id = {}
        self.board = shogi.Board()
        self.game: object = None
        self.first_game = True

    async def initialize(self) -> None:
        class UciInitializeCommand(BaseCommand[UciProtocol, None]):
            def check_initialized(self, engine: UciProtocol) -> None:
                if engine.initialized:
                    raise EngineError("engine already initialized")

            def start(self, engine: UciProtocol) -> None:
                engine.send_line("usi")

            def line_received(self, engine: UciProtocol, line: str) -> None:
                if line == "usiok" and not self.result.done():
                    engine.initialized = True
                    self.result.set_result(None)
                    self.set_finished()
                elif line.startswith("option "):
                    self._option(engine, line.split(" ", 1)[1])
                elif line.startswith("id "):
                    self._id(engine, line.split(" ", 1)[1])

            def _option(self, engine: UciProtocol, arg: str) -> None:
                current_parameter = None

                name: List[str] = []
                type: List[str] = []
                default: List[str] = []
                min = None
                max = None
                current_var = None
                var = []

                for token in arg.split(" "):
                    if token == "name" and not name:
                        current_parameter = "name"
                    elif token == "type" and not type:
                        current_parameter = "type"
                    elif token == "default" and not default:
                        current_parameter = "default"
                    elif token == "min" and min is None:
                        current_parameter = "min"
                    elif token == "max" and max is None:
                        current_parameter = "max"
                    elif token == "var":
                        current_parameter = "var"
                        if current_var is not None:
                            var.append(" ".join(current_var))
                        current_var = []
                    elif current_parameter == "name":
                        name.append(token)
                    elif current_parameter == "type":
                        type.append(token)
                    elif current_parameter == "default":
                        default.append(token)
                    elif current_parameter == "var":
                        current_var.append(token)
                    elif current_parameter == "min":
                        try:
                            min = int(token)
                        except ValueError:
                            LOGGER.exception("exception parsing option min")
                    elif current_parameter == "max":
                        try:
                            max = int(token)
                        except ValueError:
                            LOGGER.exception("exception parsing option max")

                if current_var is not None:
                    var.append(" ".join(current_var))

                without_default = Option(" ".join(name), " ".join(type), None, min, max, var)
                option = Option(without_default.name, without_default.type, without_default.parse(" ".join(default)), min, max, var)
                engine.options[option.name] = option

                if option.default is not None:
                    engine.config[option.name] = option.default
                if option.default is not None and not option.is_managed() and option.name.lower() != "usi_analysemode":
                    engine.target_config[option.name] = option.default

            def _id(self, engine: UciProtocol, arg: str) -> None:
                key, value = arg.split(" ", 1)
                engine.id[key] = value

        return await self.communicate(UciInitializeCommand)

    def _isready(self) -> None:
        self.send_line("isready")

    def _usinewgame(self) -> None:
        self.send_line("usinewgame")
        self.first_game = False

    def debug(self, on: bool = True) -> None:
        """
        Switches debug mode of the engine on or off. This does not interrupt
        other ongoing operations.
        """
        if on:
            self.send_line("debug on")
        else:
            self.send_line("debug off")

    async def ping(self) -> None:
        class UciPingCommand(BaseCommand[UciProtocol, None]):
            def start(self, engine: UciProtocol) -> None:
                engine._isready()

            def line_received(self, engine: UciProtocol, line: str) -> None:
                if line == "readyok":
                    self.result.set_result(None)
                    self.set_finished()
                else:
                    LOGGER.warning("%s: Unexpected engine output: %s", engine, line)

        return await self.communicate(UciPingCommand)

    def _setoption(self, name: str, value: ConfigValue) -> None:
        try:
            value = self.options[name].parse(value)
        except KeyError:
            raise EngineError("engine does not support option {} (available options: {})".format(name, ", ".join(self.options)))

        if value is None or value != self.config.get(name):
            builder = ["setoption name", name]
            if value is False:
                builder.append("value false")
            elif value is True:
                builder.append("value true")
            elif value is not None:
                builder.append("value")
                builder.append(str(value))

            self.send_line(" ".join(builder))
            self.config[name] = value

    def _configure(self, options: ConfigMapping) -> None:
        for name, value in collections.ChainMap(options, self.target_config).items():
            if name.lower() in MANAGED_OPTIONS:
                raise EngineError("cannot set {} which is automatically managed".format(name))
            self._setoption(name, value)

    async def configure(self, options: ConfigMapping) -> None:
        class UciConfigureCommand(BaseCommand[UciProtocol, None]):
            def start(self, engine: UciProtocol) -> None:
                engine._configure(options)
                engine.target_config.update({name: value for name, value in options.items() if value is not None})
                self.result.set_result(None)
                self.set_finished()

        return await self.communicate(UciConfigureCommand)

    def _position(self, board: shogi.Board) -> None:
        # Send starting position.
        builder = ["position"]
        root = board.root()
        fen = root.fen(shredder=false, en_passant="fen")
        if fen == shogi.STARTING_FEN:
            builder.append("startpos")
        else:
            builder.append("fen")
            builder.append(fen)

        # Send moves.
        if board.move_stack:
            builder.append("moves")
            builder.extend(move.usi() for move in board.move_stack)

        self.send_line(" ".join(builder))
        self.board = board.copy(stack=False)

    def _go(self, limit: Limit, *, root_moves: Optional[Iterable[shogi.Move]] = None, ponder: bool = False, infinite: bool = False) -> None:
        builder = ["go"]
        if ponder:
            builder.append("ponder")
        if limit.white_clock is not None:
            builder.append("wtime")
            builder.append(str(int(limit.white_clock * 1000)))
        if limit.black_clock is not None:
            builder.append("btime")
            builder.append(str(int(limit.black_clock * 1000)))
        if limit.white_inc is not None:
            builder.append("winc")
            builder.append(str(int(limit.white_inc * 1000)))
        if limit.black_inc is not None:
            builder.append("binc")
            builder.append(str(int(limit.black_inc * 1000)))
        if limit.remaining_moves is not None and int(limit.remaining_moves) > 0:
            builder.append("movestogo")
            builder.append(str(int(limit.remaining_moves)))
        if limit.depth is not None:
            builder.append("depth")
            builder.append(str(int(limit.depth)))
        if limit.nodes is not None:
            builder.append("nodes")
            builder.append(str(int(limit.nodes)))
        if limit.mate is not None:
            builder.append("mate")
            builder.append(str(int(limit.mate)))
        if limit.time is not None:
            builder.append("movetime")
            builder.append(str(int(limit.time * 1000)))
        if infinite:
            builder.append("infinite")
        if root_moves:
            builder.append("searchmoves")
            builder.extend(move.usi() for move in root_moves)
        self.send_line(" ".join(builder))

    async def play(self, board: shogi.Board, limit: Limit, *, game: object = None, info: Info = INFO_NONE, ponder: bool = False, root_moves: Optional[Iterable[shogi.Move]] = None, options: ConfigMapping = {}) -> PlayResult:
        class UciPlayCommand(BaseCommand[UciProtocol, PlayResult]):
            def start(self, engine: UciProtocol) -> None:
                self.info: InfoDict = {}
                self.pondering = False
                self.sent_isready = False

                if "USI_AnalyseMode" in engine.options and "USI_AnalyseMode" not in engine.target_config and all(name.lower() != "usi_analysemode" for name in options):
                    engine._setoption("USI_AnalyseMode", False)
                if "Ponder" in engine.options:
                    engine._setoption("Ponder", ponder)
                if "MultiPV" in engine.options:
                    engine._setoption("MultiPV", engine.options["MultiPV"].default)

                engine._configure(options)

                if engine.first_game or engine.game != game:
                    engine.game = game
                    engine._usinewgame()
                    self.sent_isready = True
                    engine._isready()
                else:
                    self._readyok(engine)

            def line_received(self, engine: UciProtocol, line: str) -> None:
                if line.startswith("info "):
                    self._info(engine, line.split(" ", 1)[1])
                elif line.startswith("bestmove "):
                    self._bestmove(engine, line.split(" ", 1)[1])
                elif line == "readyok" and self.sent_isready:
                    self._readyok(engine)
                else:
                    LOGGER.warning("%s: Unexpected engine output: %s", engine, line)

            def _readyok(self, engine: UciProtocol) -> None:
                self.sent_isready = False
                engine._position(board)
                engine._go(limit, root_moves=root_moves)

            def _info(self, engine: UciProtocol, arg: str) -> None:
                if not self.pondering:
                    self.info.update(_parse_usi_info(arg, engine.board, info))

            def _bestmove(self, engine: UciProtocol, arg: str) -> None:
                if self.pondering:
                    self.pondering = False
                elif not self.result.cancelled():
                    best = _parse_usi_bestmove(engine.board, arg)
                    self.result.set_result(PlayResult(best.move, best.ponder, self.info))

                    if ponder and best.move and best.ponder:
                        self.pondering = True
                        engine.board.push(best.move)
                        engine.board.push(best.ponder)
                        engine._position(engine.board)
                        engine._go(limit, ponder=True)

                if not self.pondering:
                    self.end(engine)

            def end(self, engine: UciProtocol) -> None:
                self.set_finished()

            def cancel(self, engine: UciProtocol) -> None:
                engine.send_line("stop")

            def engine_terminated(self, engine: UciProtocol, exc: Exception) -> None:
                # Allow terminating engine while pondering.
                if not self.result.done():
                    super().engine_terminated(engine, exc)

        return await self.communicate(UciPlayCommand)

    async def analysis(self, board: shogi.Board, limit: Optional[Limit] = None, *, multipv: Optional[int] = None, game: object = None, info: Info = INFO_ALL, root_moves: Optional[Iterable[shogi.Move]] = None, options: ConfigMapping = {}) -> "AnalysisResult":
        class UciAnalysisCommand(BaseCommand[UciProtocol, AnalysisResult]):
            def start(self, engine: UciProtocol) -> None:
                self.analysis = AnalysisResult(stop=lambda: self.cancel(engine))
                self.sent_isready = False

                if "USI_AnalyseMode" in engine.options and "USI_AnalyseMode" not in engine.target_config and all(name.lower() != "usi_analysemode" for name in options):
                    engine._setoption("USI_AnalyseMode", True)
                if "MultiPV" in engine.options or (multipv and multipv > 1):
                    engine._setoption("MultiPV", 1 if multipv is None else multipv)

                engine._configure(options)

                if engine.first_game or engine.game != game:
                    engine.game = game
                    engine._usinewgame()
                    self.sent_isready = True
                    engine._isready()
                else:
                    self._readyok(engine)

            def line_received(self, engine: UciProtocol, line: str) -> None:
                if line.startswith("info "):
                    self._info(engine, line.split(" ", 1)[1])
                elif line.startswith("bestmove "):
                    self._bestmove(engine, line.split(" ", 1)[1])
                elif line == "readyok" and self.sent_isready:
                    self._readyok(engine)
                else:
                    LOGGER.warning("%s: Unexpected engine output: %s", engine, line)

            def _readyok(self, engine: UciProtocol) -> None:
                self.sent_isready = False
                engine._position(board)

                if limit:
                    engine._go(limit, root_moves=root_moves)
                else:
                    engine._go(Limit(), root_moves=root_moves, infinite=True)

                self.result.set_result(self.analysis)

            def _info(self, engine: UciProtocol, arg: str) -> None:
                self.analysis.post(_parse_usi_info(arg, engine.board, info))

            def _bestmove(self, engine: UciProtocol, arg: str) -> None:
                if not self.result.done():
                    raise EngineError("was not searching, but engine sent bestmove")
                best = _parse_usi_bestmove(engine.board, arg)
                self.set_finished()
                self.analysis.set_finished(best)

            def cancel(self, engine: UciProtocol) -> None:
                engine.send_line("stop")

            def engine_terminated(self, engine: UciProtocol, exc: Exception) -> None:
                LOGGER.debug("%s: Closing analysis because engine has been terminated (error: %s)", engine, exc)
                self.analysis.set_exception(exc)

        return await self.communicate(UciAnalysisCommand)

    async def quit(self) -> None:
        self.send_line("quit")
        await asyncio.shield(self.returncode)


USI_REGEX = re.compile(r"^[a-h][1-8][a-h][1-8][pnbrqk]?|[PNBRQK]@[a-h][1-8]|0000\Z")

def _parse_usi_info(arg: str, root_board: shogi.Board, selector: Info = INFO_ALL) -> InfoDict:
    info: InfoDict = {}
    if not selector:
        return info

    tokens = arg.split(" ")
    while tokens:
        parameter = tokens.pop(0)

        if parameter == "string":
            info["string"] = " ".join(tokens)
            break
        elif parameter in ["depth", "seldepth", "nodes", "multipv", "currmovenumber", "hashfull", "nps", "tbhits", "cpuload"]:
            try:
                info[parameter] = int(tokens.pop(0))  # type: ignore
            except (ValueError, IndexError):
                LOGGER.error("exception parsing %s from info: %r", parameter, arg)
        elif parameter == "time":
            try:
                info["time"] = int(tokens.pop(0)) / 1000.0
            except (ValueError, IndexError):
                LOGGER.error("exception parsing %s from info: %r", parameter, arg)
        elif parameter == "ebf":
            try:
                info["ebf"] = float(tokens.pop(0))
            except (ValueError, IndexError):
                LOGGER.error("exception parsing %s from info: %r", parameter, arg)
        elif parameter == "score" and selector & INFO_SCORE:
            try:
                kind = tokens.pop(0)
                value = tokens.pop(0)
                if tokens and tokens[0] in ["lowerbound", "upperbound"]:
                    info[tokens.pop(0)] = True  # type: ignore
                if kind == "cp":
                    info["score"] = PovScore(Cp(int(value)), root_board.turn)
                elif kind == "mate":
                    info["score"] = PovScore(Mate(int(value)), root_board.turn)
                else:
                    LOGGER.error("unknown score kind %r in info (expected cp or mate): %r", kind, arg)
            except (ValueError, IndexError):
                LOGGER.error("exception parsing score from info: %r", arg)
        elif parameter == "currmove":
            try:
                info["currmove"] = shogi.Move.from_usi(tokens.pop(0))
            except (ValueError, IndexError):
                LOGGER.error("exception parsing currmove from info: %r", arg)
        elif parameter == "currline" and selector & INFO_CURRLINE:
            try:
                if "currline" not in info:
                    info["currline"] = {}

                cpunr = int(tokens.pop(0))
                currline: List[shogi.Move] = []

                board = root_board.copy(stack=False)
                while tokens and USI_REGEX.match(tokens[0]):
                    currline.append(board.push_usi(tokens.pop(0)))
                info["currline"][cpunr] = currline
            except (ValueError, IndexError):
                LOGGER.error("exception parsing currline from info: %r, position at root: %s", arg, root_board.fen())
        elif parameter == "refutation" and selector & INFO_REFUTATION:
            try:
                if "refutation" not in info:
                    info["refutation"] = {}

                board = root_board.copy(stack=False)
                refuted = board.push_usi(tokens.pop(0))
                refuted_by: List[shogi.Move] = []

                while tokens and USI_REGEX.match(tokens[0]):
                    refuted_by.append(board.push_usi(tokens.pop(0)))
                info["refutation"][refuted] = refuted_by
            except (ValueError, IndexError):
                LOGGER.error("exception parsing refutation from info: %r, position at root: %s", arg, root_board.fen())
        elif parameter == "pv" and selector & INFO_PV:
            try:
                pv: List[shogi.Move] = []
                board = root_board.copy(stack=False)
                while tokens and USI_REGEX.match(tokens[0]):
                    pv.append(board.push_usi(tokens.pop(0)))
                info["pv"] = pv
            except (ValueError, IndexError):
                LOGGER.error("exception parsing pv from info: %r, position at root: %s", arg, root_board.fen())
        elif parameter == "wdl":
            try:
                info["wdl"] = int(tokens.pop(0)), int(tokens.pop(0)), int(tokens.pop(0))
            except (ValueError, IndexError):
                LOGGER.error("exception parsing wdl from info: %r", arg)

    return info

def _parse_usi_bestmove(board: shogi.Board, args: str) -> "BestMove":
    tokens = args.split(None, 2)

    move = None
    ponder = None
    if tokens[0] != "(none)":
        try:
            move = board.push_usi(tokens[0])
        except ValueError as err:
            raise EngineError(err)

        try:
            if len(tokens) >= 3 and tokens[1] == "ponder" and tokens[2] != "(none)":
                ponder = board.parse_usi(tokens[2])
        except ValueError:
            LOGGER.exception("engine sent invalid ponder move")
        finally:
            board.pop()

    return BestMove(move, ponder)


class UciOptionMap(MutableMapping[str, T]):
    """Dictionary with case-insensitive keys."""

    def __init__(self, data: Optional[Union[Iterable[Tuple[str, T]]]] = None, **kwargs: T) -> None:
        self._store: Dict[str, Tuple[str, T]] = {}
        if data is None:
            data = {}
        self.update(data, **kwargs)

    def __setitem__(self, key: str, value: T) -> None:
        self._store[key.lower()] = (key, value)

    def __getitem__(self, key: str) -> T:
        return self._store[key.lower()][1]

    def __delitem__(self, key: str) -> None:
        del self._store[key.lower()]

    def __iter__(self) -> Iterator[str]:
        return (casedkey for casedkey, mappedvalue in self._store.values())

    def __len__(self) -> int:
        return len(self._store)

    def __eq__(self, other: object) -> bool:
        try:
            for key, value in self.items():
                if key not in other or other[key] != value:  # type: ignore
                    return False

            for key, value in other.items():  # type: ignore
                if key not in self or self[key] != value:
                    return False

            return True
        except (TypeError, AttributeError):
            return NotImplemented

    def copy(self) -> "UciOptionMap[T]":
        return type(self)(self._store.values())

    def __copy__(self) -> "UciOptionMap[T]":
        return self.copy()

    def __repr__(self) -> str:
        return f"{type(self).__name__}({dict(self.items())!r})"



class BestMove:
    """Returned by :func:`shogi.engine.AnalysisResult.wait()`."""

    def __init__(self, move: Optional[shogi.Move], ponder: Optional[shogi.Move]):
        self.move = move
        self.ponder = ponder

    def __repr__(self) -> str:
        return "<{} at {:#x} (move={}, ponder={}>".format(
            type(self).__name__, id(self), self.move, self.ponder)


class AnalysisResult:
    """
    Handle to ongoing engine analysis.
    Returned by :func:`shogi.engine.EngineProtocol.analysis()`.

    Can be used to asynchronously iterate over information sent by the engine.

    Automatically stops the analysis when used as a context manager.
    """

    def __init__(self, stop: Optional[Callable[[], None]] = None):
        self._stop = stop
        self._queue: asyncio.Queue[InfoDict] = asyncio.Queue()
        self._posted_kork = False
        self._seen_kork = False
        self._finished: asyncio.Future[BestMove] = asyncio.Future()
        self.multipv: List[InfoDict] = [{}]

    def post(self, info: InfoDict) -> None:
        # Empty dictionary reserved for kork.
        if not info:
            return

        multipv = typing.cast(int, info.get("multipv", 1))
        while len(self.multipv) < multipv:
            self.multipv.append({})
        self.multipv[multipv - 1].update(info)

        self._queue.put_nowait(info)

    def _kork(self):
        if not self._posted_kork:
            self._posted_kork = True
            self._queue.put_nowait({})

    def set_finished(self, best: BestMove) -> None:
        if not self._finished.done():
            self._finished.set_result(best)
        self._kork()

    def set_exception(self, exc: Exception) -> None:
        self._finished.set_exception(exc)
        self._kork()

    @property
    def info(self) -> InfoDict:
        return self.multipv[0]

    def stop(self) -> None:
        """Stops the analysis as soon as possible."""
        if self._stop and not self._posted_kork:
            self._stop()
            self._stop = None

    async def wait(self) -> BestMove:
        """Waits until the analysis is complete (or stopped)."""
        return await self._finished

    async def get(self) -> InfoDict:
        """
        Waits for the next dictionary of information from the engine and
        returns it.

        It might be more convenient to use ``async for info in analysis: ...``.

        :raises: :exc:`shogi.engine.AnalysisComplete` if the analysis is
            complete (or has been stopped) and all information has been
            consumed. Use :func:`~shogi.engine.AnalysisResult.next()` if you
            prefer to get ``None`` instead of an exception.
        """
        if self._seen_kork:
            raise AnalysisComplete()

        info = await self._queue.get()
        if not info:
            # Empty dictionary marks end.
            self._seen_kork = True
            await self._finished
            raise AnalysisComplete()

        return info

    def empty(self) -> bool:
        """
        Checks if all information has been consumed.

        If the queue is empty, but the analysis is still ongoing, then further
        information can become available in the future.

        If the queue is not empty, then the next call to
        :func:`~shogi.engine.AnalysisResult.get()` will return instantly.
        """
        return self._seen_kork or self._queue.qsize() <= self._posted_kork

    async def next(self) -> Optional[InfoDict]:
        try:
            return await self.get()
        except AnalysisComplete:
            return None

    def __aiter__(self) -> "AnalysisResult":
        return self

    async def __anext__(self) -> InfoDict:
        try:
            return await self.get()
        except AnalysisComplete:
            raise StopAsyncIteration

    def __enter__(self) -> "AnalysisResult":
        return self

    def __exit__(self, exc_type: Optional[Type[BaseException]], exc_value: Optional[BaseException], traceback: Optional[TracebackType]) -> None:
        self.stop()


async def popen_usi(command: Union[str, List[str]], *, setpgrp: bool = False, **popen_args: Any) -> Tuple[asyncio.SubprocessTransport, UciProtocol]:
    """
    Spawns and initializes a USI engine.

    :param command: Path of the engine executable, or a list including the
        path and arguments.
    :param setpgrp: Open the engine process in a new process group. This will
        stop signals (such as keyboard interrupts) from propagating from the
        parent process. Defaults to ``False``.
    :param popen_args: Additional arguments for
        `popen <https://docs.python.org/3/library/subprocess.html#popen-constructor>`_.
        Do not set ``stdin``, ``stdout``, ``bufsize`` or
        ``universal_newlines``.

    Returns a subprocess transport and engine protocol pair.
    """
    transport, protocol = await UciProtocol.popen(command, setpgrp=setpgrp, **popen_args)
    try:
        await protocol.initialize()
    except:
        transport.close()
        raise
    return transport, protocol


class SimpleEngine:
    """
    Synchronous wrapper around a transport and engine protocol pair. Provides
    the same methods and attributes as :class:`~shogi.engine.EngineProtocol`
    with blocking functions instead of coroutines.

    You may not concurrently modify objects passed to any of the methods. Other
    than that, :class:`~shogi.engine.SimpleEngine` is thread-safe. When sending
    a new command to the engine, any previous running command will be cancelled
    as soon as possible.

    Methods will raise :class:`asyncio.TimeoutError` if an operation takes
    *timeout* seconds longer than expected (unless *timeout* is ``None``).

    Automatically closes the transport when used as a context manager.
    """

    def __init__(self, transport: asyncio.SubprocessTransport, protocol: EngineProtocol, *, timeout: Optional[float] = 10.0) -> None:
        self.transport = transport
        self.protocol = protocol
        self.timeout = timeout

        self._shutdown_lock = threading.Lock()
        self._shutdown = False
        self.shutdown_event = asyncio.Event()

        self.returncode: concurrent.futures.Future[int] = concurrent.futures.Future()

    def _timeout_for(self, limit: Optional[Limit]) -> Optional[float]:
        if self.timeout is None or limit is None or limit.time is None:
            return None
        return self.timeout + limit.time

    @contextlib.contextmanager
    def _not_shut_down(self) -> Generator[None, None, None]:
        with self._shutdown_lock:
            if self._shutdown:
                raise EngineTerminatedError("engine event loop dead")
            yield

    @property
    def options(self) -> MutableMapping[str, Option]:
        async def _get() -> MutableMapping[str, Option]:
            return copy.copy(self.protocol.options)

        with self._not_shut_down():
            future = asyncio.run_coroutine_threadsafe(_get(), self.protocol.loop)
        return future.result()

    @property
    def id(self) -> Mapping[str, str]:
        async def _get() -> Mapping[str, str]:
            return self.protocol.id.copy()

        with self._not_shut_down():
            future = asyncio.run_coroutine_threadsafe(_get(), self.protocol.loop)
        return future.result()

    def communicate(self, command_factory: Callable[[], BaseCommand[EngineProtocol, T]]) -> T:
        with self._not_shut_down():
            coro = self.protocol.communicate(command_factory)
            future = asyncio.run_coroutine_threadsafe(coro, self.protocol.loop)
        return future.result()

    def configure(self, options: ConfigMapping) -> None:
        with self._not_shut_down():
            coro = asyncio.wait_for(self.protocol.configure(options), self.timeout)
            future = asyncio.run_coroutine_threadsafe(coro, self.protocol.loop)
        return future.result()

    def ping(self) -> None:
        with self._not_shut_down():
            coro = asyncio.wait_for(self.protocol.ping(), self.timeout)
            future = asyncio.run_coroutine_threadsafe(coro, self.protocol.loop)
        return future.result()

    def play(self, board: shogi.Board, limit: Limit, *, game: object = None, info: Info = INFO_NONE, ponder: bool = False, root_moves: Optional[Iterable[shogi.Move]] = None, options: ConfigMapping = {}) -> PlayResult:
        with self._not_shut_down():
            coro = asyncio.wait_for(
                self.protocol.play(board, limit, game=game, info=info, ponder=ponder, root_moves=root_moves, options=options),
                self._timeout_for(limit))
            future = asyncio.run_coroutine_threadsafe(coro, self.protocol.loop)
        return future.result()

    @typing.overload
    def analyse(self, board: shogi.Board, limit: Limit, *, game: object = None, info: Info = INFO_ALL, root_moves: Optional[Iterable[shogi.Move]] = None, options: ConfigMapping = {}) -> InfoDict: ...
    @typing.overload
    def analyse(self, board: shogi.Board, limit: Limit, *, multipv: int, game: object = None, info: Info = INFO_ALL, root_moves: Optional[Iterable[shogi.Move]] = None, options: ConfigMapping = {}) -> List[InfoDict]: ...
    @typing.overload
    def analyse(self, board: shogi.Board, limit: Limit, *, multipv: Optional[int] = None, game: object = None, info: Info = INFO_ALL, root_moves: Optional[Iterable[shogi.Move]] = None, options: ConfigMapping = {}) -> Union[InfoDict, List[InfoDict]]: ...
    def analyse(self, board: shogi.Board, limit: Limit, *, multipv: Optional[int] = None, game: object = None, info: Info = INFO_ALL, root_moves: Optional[Iterable[shogi.Move]] = None, options: ConfigMapping = {}) -> Union[InfoDict, List[InfoDict]]:
        with self._not_shut_down():
            coro = asyncio.wait_for(
                self.protocol.analyse(board, limit, multipv=multipv, game=game, info=info, root_moves=root_moves, options=options),
                self._timeout_for(limit))
            future = asyncio.run_coroutine_threadsafe(coro, self.protocol.loop)
        return future.result()

    def analysis(self, board: shogi.Board, limit: Optional[Limit] = None, *, multipv: Optional[int] = None, game: object = None, info: Info = INFO_ALL, root_moves: Optional[Iterable[shogi.Move]] = None, options: ConfigMapping = {}) -> "SimpleAnalysisResult":
        with self._not_shut_down():
            coro = asyncio.wait_for(
                self.protocol.analysis(board, limit, multipv=multipv, game=game, info=info, root_moves=root_moves, options=options),
                self.timeout)  # Analyis should start immediately
            future = asyncio.run_coroutine_threadsafe(coro, self.protocol.loop)
        return SimpleAnalysisResult(self, future.result())

    def quit(self) -> None:
        with self._not_shut_down():
            coro = asyncio.wait_for(self.protocol.quit(), self.timeout)
            future = asyncio.run_coroutine_threadsafe(coro, self.protocol.loop)
        return future.result()

    def close(self) -> None:
        """
        Closes the transport and the background event loop as soon as possible.
        """
        def _shutdown() -> None:
            self.transport.close()
            self.shutdown_event.set()

        with self._shutdown_lock:
            if not self._shutdown:
                self._shutdown = True
                self.protocol.loop.call_soon_threadsafe(_shutdown)

    @classmethod
    def popen(cls, Protocol: Type[EngineProtocol], command: Union[str, List[str]], *, timeout: Optional[float] = 10.0, debug: bool = False, setpgrp: bool = False, **popen_args: Any) -> "SimpleEngine":
        async def background(future: "concurrent.futures.Future[SimpleEngine]") -> None:
            transport, protocol = await Protocol.popen(command, setpgrp=setpgrp, **popen_args)
            threading.current_thread().name = f"{cls.__name__} (pid={transport.get_pid()})"
            simple_engine = cls(transport, protocol, timeout=timeout)
            try:
                await asyncio.wait_for(protocol.initialize(), timeout)
                future.set_result(simple_engine)
                returncode = await protocol.returncode
                simple_engine.returncode.set_result(returncode)
            finally:
                simple_engine.close()
            await simple_engine.shutdown_event.wait()

        return run_in_background(background, name=f"{cls.__name__} (command={command!r})", debug=debug)

    @classmethod
    def popen_usi(cls, command: Union[str, List[str]], *, timeout: Optional[float] = 10.0, debug: bool = False, setpgrp: bool = False, **popen_args: Any) -> "SimpleEngine":
        """
        Spawns and initializes a USI engine.
        Returns a :class:`~shogi.engine.SimpleEngine` instance.
        """
        return cls.popen(UciProtocol, command, timeout=timeout, debug=debug, setpgrp=setpgrp, **popen_args)

    def __enter__(self) -> "SimpleEngine":
        return self

    def __exit__(self, exc_type: Optional[Type[BaseException]], exc_value: Optional[BaseException], traceback: Optional[TracebackType]) -> None:
        self.close()

    def __repr__(self) -> str:
        pid = self.transport.get_pid()  # This happens to be thread-safe
        return f"<{type(self).__name__} (pid={pid})>"


class SimpleAnalysisResult:
    """
    Synchronous wrapper around :class:`~shogi.engine.AnalysisResult`. Returned
    by :func:`shogi.engine.SimpleEngine.analysis()`.
    """

    def __init__(self, simple_engine: SimpleEngine, inner: AnalysisResult) -> None:
        self.simple_engine = simple_engine
        self.inner = inner

    @property
    def info(self) -> InfoDict:
        async def _get() -> InfoDict:
            return self.inner.info.copy()

        with self.simple_engine._not_shut_down():
            future = asyncio.run_coroutine_threadsafe(_get(), self.simple_engine.protocol.loop)
        return future.result()

    @property
    def multipv(self) -> List[InfoDict]:
        async def _get() -> List[InfoDict]:
            return [info.copy() for info in self.inner.multipv]

        with self.simple_engine._not_shut_down():
            future = asyncio.run_coroutine_threadsafe(_get(), self.simple_engine.protocol.loop)
        return future.result()

    def stop(self) -> None:
        with self.simple_engine._not_shut_down():
            self.simple_engine.protocol.loop.call_soon_threadsafe(self.inner.stop)

    def wait(self) -> BestMove:
        with self.simple_engine._not_shut_down():
            future = asyncio.run_coroutine_threadsafe(self.inner.wait(), self.simple_engine.protocol.loop)
        return future.result()

    def empty(self) -> bool:
        async def _empty() -> bool:
            return self.inner.empty()

        with self.simple_engine._not_shut_down():
            future = asyncio.run_coroutine_threadsafe(_empty(), self.simple_engine.protocol.loop)
        return future.result()

    def get(self) -> InfoDict:
        with self.simple_engine._not_shut_down():
            future = asyncio.run_coroutine_threadsafe(self.inner.get(), self.simple_engine.protocol.loop)
        return future.result()

    def next(self) -> Optional[InfoDict]:
        with self.simple_engine._not_shut_down():
            future = asyncio.run_coroutine_threadsafe(self.inner.next(), self.simple_engine.protocol.loop)
        return future.result()

    def __iter__(self) -> Iterator[InfoDict]:
        with self.simple_engine._not_shut_down():
            self.simple_engine.protocol.loop.call_soon_threadsafe(self.inner.__aiter__)
        return self

    def __next__(self) -> InfoDict:
        try:
            with self.simple_engine._not_shut_down():
                future = asyncio.run_coroutine_threadsafe(self.inner.__anext__(), self.simple_engine.protocol.loop)
            return future.result()
        except StopAsyncIteration:
            raise StopIteration

    def __enter__(self) -> "SimpleAnalysisResult":
        return self

    def __exit__(self, exc_type: Optional[Type[BaseException]], exc_value: Optional[BaseException], traceback: Optional[TracebackType]) -> None:
        self.stop()
