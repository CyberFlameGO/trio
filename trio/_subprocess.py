# coding: utf-8

import os
import subprocess
import sys
from typing import Optional
from functools import partial
import warnings
from typing import TYPE_CHECKING

from ._abc import AsyncResource, SendStream, ReceiveStream
from ._highlevel_generic import StapledStream
from ._sync import Lock
from ._subprocess_platform import (
    wait_child_exiting,
    create_pipe_to_child_stdin,
    create_pipe_from_child_output,
)
from ._deprecate import deprecated
from ._util import NoPublicConstructor
import trio

# Linux-specific, but has complex lifetime management stuff so we hard-code it
# here instead of hiding it behind the _subprocess_platform abstraction
can_try_pidfd_open: bool
if TYPE_CHECKING:

    def pidfd_open(fd: int, flags: int) -> int:
        ...


else:
    can_try_pidfd_open = True
    try:
        from os import pidfd_open
    except ImportError:
        if sys.platform == "linux":
            import ctypes

            _cdll_for_pidfd_open = ctypes.CDLL(None, use_errno=True)
            _cdll_for_pidfd_open.syscall.restype = ctypes.c_long
            # pid and flags are actually int-sized, but the syscall() function
            # always takes longs. (Except on x32 where long is 32-bits and syscall
            # takes 64-bit arguments. But in the unlikely case that anyone is
            # using x32, this will still work, b/c we only need to pass in 32 bits
            # of data, and the C ABI doesn't distinguish between passing 32-bit vs
            # 64-bit integers; our 32-bit values will get loaded into 64-bit
            # registers where syscall() will find them.)
            _cdll_for_pidfd_open.syscall.argtypes = [
                ctypes.c_long,  # syscall number
                ctypes.c_long,  # pid
                ctypes.c_long,  # flags
            ]
            __NR_pidfd_open = 434

            def pidfd_open(fd: int, flags: int) -> int:
                result = _cdll_for_pidfd_open.syscall(__NR_pidfd_open, fd, flags)
                if result < 0:
                    err = ctypes.get_errno()
                    raise OSError(err, os.strerror(err))
                return result

        else:
            can_try_pidfd_open = False


class Process(AsyncResource, metaclass=NoPublicConstructor):
    r"""A child process. Like :class:`subprocess.Popen`, but async.

    This class has no public constructor. The most common way to get a
    `Process` object is to combine `Nursery.start` with `run_process`::

       process_object = await nursery.start(run_process, ...)

    This way, `run_process` supervises the process and makes sure that it is
    cleaned up properly, while optionally checking the return value, feeding
    it input, and so on.

    If you need more control – for example, because you want to spawn a child
    process that outlives your program – then another option is to use
    `trio.lowlevel.open_process`::

       process_object = await trio.lowlevel.open_process(...)

    Attributes:
      args (str or list): The ``command`` passed at construction time,
          specifying the process to execute and its arguments.
      pid (int): The process ID of the child process managed by this object.
      stdin (trio.abc.SendStream or None): A stream connected to the child's
          standard input stream: when you write bytes here, they become available
          for the child to read. Only available if the :class:`Process`
          was constructed using ``stdin=PIPE``; otherwise this will be None.
      stdout (trio.abc.ReceiveStream or None): A stream connected to
          the child's standard output stream: when the child writes to
          standard output, the written bytes become available for you
          to read here. Only available if the :class:`Process` was
          constructed using ``stdout=PIPE``; otherwise this will be None.
      stderr (trio.abc.ReceiveStream or None): A stream connected to
          the child's standard error stream: when the child writes to
          standard error, the written bytes become available for you
          to read here. Only available if the :class:`Process` was
          constructed using ``stderr=PIPE``; otherwise this will be None.
      stdio (trio.StapledStream or None): A stream that sends data to
          the child's standard input and receives from the child's standard
          output. Only available if both :attr:`stdin` and :attr:`stdout` are
          available; otherwise this will be None.

    """

    universal_newlines = False
    encoding = None
    errors = None

    # Available for the per-platform wait_child_exiting() implementations
    # to stash some state; waitid platforms use this to avoid spawning
    # arbitrarily many threads if wait() keeps getting cancelled.
    _wait_for_exit_data = None

    def __init__(self, popen, stdin, stdout, stderr):
        self._proc = popen
        self.stdin = stdin  # type: Optional[SendStream]
        self.stdout = stdout  # type: Optional[ReceiveStream]
        self.stderr = stderr  # type: Optional[ReceiveStream]

        self.stdio = None  # type: Optional[StapledStream]
        if self.stdin is not None and self.stdout is not None:
            self.stdio = StapledStream(self.stdin, self.stdout)

        self._wait_lock = Lock()

        self._pidfd = None
        if can_try_pidfd_open:
            try:
                fd = pidfd_open(self._proc.pid, 0)
            except OSError:
                # Well, we tried, but it didn't work (probably because we're
                # running on an older kernel, or in an older sandbox, that
                # hasn't been updated to support pidfd_open). We'll fall back
                # on waitid instead.
                pass
            else:
                # It worked! Wrap the raw fd up in a Python file object to
                # make sure it'll get closed.
                self._pidfd = open(fd)

        self.args = self._proc.args
        self.pid = self._proc.pid

    def __repr__(self):
        returncode = self.returncode
        if returncode is None:
            status = "running with PID {}".format(self.pid)
        else:
            if returncode < 0:
                status = "exited with signal {}".format(-returncode)
            else:
                status = "exited with status {}".format(returncode)
        return "<trio.Process {!r}: {}>".format(self.args, status)

    @property
    def returncode(self):
        """The exit status of the process (an integer), or ``None`` if it's
        still running.

        By convention, a return code of zero indicates success.  On
        UNIX, negative values indicate termination due to a signal,
        e.g., -11 if terminated by signal 11 (``SIGSEGV``).  On
        Windows, a process that exits due to a call to
        :meth:`Process.terminate` will have an exit status of 1.

        Unlike the standard library `subprocess.Popen.returncode`, you don't
        have to call `poll` or `wait` to update this attribute; it's
        automatically updated as needed, and will always give you the latest
        information.

        """
        result = self._proc.poll()
        if result is not None:
            self._close_pidfd()
        return result

    @deprecated(
        "0.20.0",
        thing="using trio.Process as an async context manager",
        issue=1104,
        instead="run_process or nursery.start(run_process, ...)",
    )
    async def __aenter__(self):
        return self

    @deprecated(
        "0.20.0", issue=1104, instead="run_process or nursery.start(run_process, ...)"
    )
    async def aclose(self):
        """Close any pipes we have to the process (both input and output)
        and wait for it to exit.

        If cancelled, kills the process and waits for it to finish
        exiting before propagating the cancellation.
        """
        with trio.CancelScope(shield=True):
            if self.stdin is not None:
                await self.stdin.aclose()
            if self.stdout is not None:
                await self.stdout.aclose()
            if self.stderr is not None:
                await self.stderr.aclose()
        try:
            await self.wait()
        finally:
            if self._proc.returncode is None:
                self.kill()
                with trio.CancelScope(shield=True):
                    await self.wait()

    def _close_pidfd(self):
        if self._pidfd is not None:
            self._pidfd.close()
            self._pidfd = None

    async def wait(self):
        """Block until the process exits.

        Returns:
          The exit status of the process; see :attr:`returncode`.
        """
        async with self._wait_lock:
            if self.poll() is None:
                if self._pidfd is not None:
                    await trio.lowlevel.wait_readable(self._pidfd)
                else:
                    await wait_child_exiting(self)
                # We have to use .wait() here, not .poll(), because on macOS
                # (and maybe other systems, who knows), there's a race
                # condition inside the kernel that creates a tiny window where
                # kqueue reports that the process has exited, but
                # waitpid(WNOHANG) can't yet reap it. So this .wait() may
                # actually block for a tiny fraction of a second.
                self._proc.wait()
                self._close_pidfd()
        assert self._proc.returncode is not None
        return self._proc.returncode

    def poll(self):
        """Returns the exit status of the process (an integer), or ``None`` if
        it's still running.

        Note that on Trio (unlike the standard library `subprocess.Popen`),
        ``process.poll()`` and ``process.returncode`` always give the same
        result. See `returncode` for more details. This method is only
        included to make it easier to port code from `subprocess`.

        """
        return self.returncode

    def send_signal(self, sig):
        """Send signal ``sig`` to the process.

        On UNIX, ``sig`` may be any signal defined in the
        :mod:`signal` module, such as ``signal.SIGINT`` or
        ``signal.SIGTERM``. On Windows, it may be anything accepted by
        the standard library :meth:`subprocess.Popen.send_signal`.
        """
        self._proc.send_signal(sig)

    def terminate(self):
        """Terminate the process, politely if possible.

        On UNIX, this is equivalent to
        ``send_signal(signal.SIGTERM)``; by convention this requests
        graceful termination, but a misbehaving or buggy process might
        ignore it. On Windows, :meth:`terminate` forcibly terminates the
        process in the same manner as :meth:`kill`.
        """
        self._proc.terminate()

    def kill(self):
        """Immediately terminate the process.

        On UNIX, this is equivalent to
        ``send_signal(signal.SIGKILL)``.  On Windows, it calls
        ``TerminateProcess``. In both cases, the process cannot
        prevent itself from being killed, but the termination will be
        delivered asynchronously; use :meth:`wait` if you want to
        ensure the process is actually dead before proceeding.
        """
        self._proc.kill()


async def open_process(
    command, *, stdin=None, stdout=None, stderr=None, **options
) -> Process:
    r"""Execute a child program in a new process.

    After construction, you can interact with the child process by writing data to its
    `~trio.Process.stdin` stream (a `~trio.abc.SendStream`), reading data from its
    `~trio.Process.stdout` and/or `~trio.Process.stderr` streams (both
    `~trio.abc.ReceiveStream`\s), sending it signals using `~trio.Process.terminate`,
    `~trio.Process.kill`, or `~trio.Process.send_signal`, and waiting for it to exit
    using `~trio.Process.wait`. See `trio.Process` for details.

    Each standard stream is only available if you specify that a pipe should be created
    for it. For example, if you pass ``stdin=subprocess.PIPE``, you can write to the
    `~trio.Process.stdin` stream, else `~trio.Process.stdin` will be ``None``.

    Unlike `trio.run_process`, this function doesn't do any kind of automatic
    management of the child process. It's up to you to implement whatever semantics you
    want.

    Args:
      command (list or str): The command to run. Typically this is a
          sequence of strings such as ``['ls', '-l', 'directory with spaces']``,
          where the first element names the executable to invoke and the other
          elements specify its arguments. With ``shell=True`` in the
          ``**options``, or on Windows, ``command`` may alternatively
          be a string, which will be parsed following platform-dependent
          :ref:`quoting rules <subprocess-quoting>`.
      stdin: Specifies what the child process's standard input
          stream should connect to: output written by the parent
          (``subprocess.PIPE``), nothing (``subprocess.DEVNULL``),
          or an open file (pass a file descriptor or something whose
          ``fileno`` method returns one). If ``stdin`` is unspecified,
          the child process will have the same standard input stream
          as its parent.
      stdout: Like ``stdin``, but for the child process's standard output
          stream.
      stderr: Like ``stdin``, but for the child process's standard error
          stream. An additional value ``subprocess.STDOUT`` is supported,
          which causes the child's standard output and standard error
          messages to be intermixed on a single standard output stream,
          attached to whatever the ``stdout`` option says to attach it to.
      **options: Other :ref:`general subprocess options <subprocess-options>`
          are also accepted.

    Returns:
      A new `trio.Process` object.

    Raises:
      OSError: if the process spawning fails, for example because the
         specified command could not be found.

    """
    for key in ("universal_newlines", "text", "encoding", "errors", "bufsize"):
        if options.get(key):
            raise TypeError(
                "trio.Process only supports communicating over "
                "unbuffered byte streams; the '{}' option is not supported".format(key)
            )

    if os.name == "posix":
        if isinstance(command, str) and not options.get("shell"):
            raise TypeError(
                "command must be a sequence (not a string) if shell=False "
                "on UNIX systems"
            )
        if not isinstance(command, str) and options.get("shell"):
            raise TypeError(
                "command must be a string (not a sequence) if shell=True "
                "on UNIX systems"
            )

    trio_stdin = None  # type: Optional[SendStream]
    trio_stdout = None  # type: Optional[ReceiveStream]
    trio_stderr = None  # type: Optional[ReceiveStream]

    if stdin == subprocess.PIPE:
        trio_stdin, stdin = create_pipe_to_child_stdin()
    if stdout == subprocess.PIPE:
        trio_stdout, stdout = create_pipe_from_child_output()
    if stderr == subprocess.STDOUT:
        # If we created a pipe for stdout, pass the same pipe for
        # stderr.  If stdout was some non-pipe thing (DEVNULL or a
        # given FD), pass the same thing. If stdout was passed as
        # None, keep stderr as STDOUT to allow subprocess to dup
        # our stdout. Regardless of which of these is applicable,
        # don't create a new Trio stream for stderr -- if stdout
        # is piped, stderr will be intermixed on the stdout stream.
        if stdout is not None:
            stderr = stdout
    elif stderr == subprocess.PIPE:
        trio_stderr, stderr = create_pipe_from_child_output()

    try:
        popen = await trio.to_thread.run_sync(
            partial(
                subprocess.Popen,
                command,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                **options,
            )
        )
    finally:
        # Close the parent's handle for each child side of a pipe;
        # we want the child to have the only copy, so that when
        # it exits we can read EOF on our side.
        if trio_stdin is not None:
            os.close(stdin)
        if trio_stdout is not None:
            os.close(stdout)
        if trio_stderr is not None:
            os.close(stderr)

    return Process._create(popen, trio_stdin, trio_stdout, trio_stderr)


async def _windows_deliver_cancel(p):
    try:
        p.terminate()
    except OSError as exc:
        warnings.warn(RuntimeWarning(f"TerminateProcess on {p!r} failed with: {exc!r}"))


async def _posix_deliver_cancel(p):
    try:
        p.terminate()
        await trio.sleep(5)
        warnings.warn(
            RuntimeWarning(
                f"process {p!r} ignored SIGTERM for 5 seconds. "
                f"(Maybe you should pass a custom deliver_cancel?) "
                f"Trying SIGKILL."
            )
        )
        p.kill()
    except OSError as exc:
        warnings.warn(
            RuntimeWarning(f"tried to kill process {p!r}, but failed with: {exc!r}")
        )


async def run_process(
    command,
    *,
    stdin=b"",
    capture_stdout=False,
    capture_stderr=False,
    check=True,
    deliver_cancel=None,
    task_status=trio.TASK_STATUS_IGNORED,
    **options,
):
    """Run ``command`` in a subprocess and wait for it to complete.

    This function can be called in two different ways.

    One option is a direct call, like::

        completed_process_info = await trio.run_process(...)

    In this case, it returns a :class:`subprocess.CompletedProcess` instance
    describing the results. Use this if you want to treat a process like a
    function call.

    The other option is to run it as a task using `Nursery.start` – the enhanced version
    of `~Nursery.start_soon` that lets a task pass back a value during startup::

        process = await nursery.start(trio.run_process, ...)

    In this case, `~Nursery.start` returns a `Process` object that you can use
    to interact with the process while it's running. Use this if you want to
    treat a process like a background task.

    Either way, `run_process` makes sure that the process has exited before
    returning, handles cancellation, optionally checks for errors, and
    provides some convenient shorthands for dealing with the child's
    input/output.

    **Input:** `run_process` supports all the same ``stdin=`` arguments as
    `subprocess.Popen`. In addition, if you simply want to pass in some fixed
    data, you can pass a plain `bytes` object, and `run_process` will take
    care of setting up a pipe, feeding in the data you gave, and then sending
    end-of-file. The default is ``b""``, which means that the child will receive
    an empty stdin. If you want the child to instead read from the parent's
    stdin, use ``stdin=None``.

    **Output:** By default, any output produced by the subprocess is
    passed through to the standard output and error streams of the
    parent Trio process.

    When calling `run_process` directly, you can capture the subprocess's output by
    passing ``capture_stdout=True`` to capture the subprocess's standard output, and/or
    ``capture_stderr=True`` to capture its standard error. Captured data is collected up
    by Trio into an in-memory buffer, and then provided as the
    :attr:`~subprocess.CompletedProcess.stdout` and/or
    :attr:`~subprocess.CompletedProcess.stderr` attributes of the returned
    :class:`~subprocess.CompletedProcess` object. The value for any stream that was not
    captured will be ``None``.

    If you want to capture both stdout and stderr while keeping them
    separate, pass ``capture_stdout=True, capture_stderr=True``.

    If you want to capture both stdout and stderr but mixed together
    in the order they were printed, use: ``capture_stdout=True, stderr=subprocess.STDOUT``.
    This directs the child's stderr into its stdout, so the combined
    output will be available in the `~subprocess.CompletedProcess.stdout`
    attribute.

    If you're using ``await nursery.start(trio.run_process, ...)`` and want to capture
    the subprocess's output for further processing, then use ``stdout=subprocess.PIPE``
    and then make sure to read the data out of the `Process.stdout` stream. If you want
    to capture stderr separately, use ``stderr=subprocess.PIPE``. If you want to capture
    both, but mixed together in the correct order, use ``stdout=subproces.PIPE,
    stderr=subprocess.STDOUT``.

    **Error checking:** If the subprocess exits with a nonzero status
    code, indicating failure, :func:`run_process` raises a
    :exc:`subprocess.CalledProcessError` exception rather than
    returning normally. The captured outputs are still available as
    the :attr:`~subprocess.CalledProcessError.stdout` and
    :attr:`~subprocess.CalledProcessError.stderr` attributes of that
    exception.  To disable this behavior, so that :func:`run_process`
    returns normally even if the subprocess exits abnormally, pass ``check=False``.

    Note that this can make the ``capture_stdout`` and ``capture_stderr``
    arguments useful even when starting `run_process` as a task: if you only
    care about the output if the process fails, then you can enable capturing
    and then read the output off of the `~subprocess.CalledProcessError`.

    **Cancellation:** If cancelled, `run_process` sends a termination
    request to the subprocess, then waits for it to fully exit. The
    ``deliver_cancel`` argument lets you control how the process is terminated.

    .. note:: `run_process` is intentionally similar to the standard library
       `subprocess.run`, but some of the defaults are different. Specifically, we
       default to:

       - ``check=True``, because `"errors should never pass silently / unless
         explicitly silenced" <https://www.python.org/dev/peps/pep-0020/>`__.

       - ``stdin=b""``, because it produces less-confusing results if a subprocess
         unexpectedly tries to read from stdin.

       To get the `subprocess.run` semantics, use ``check=False, stdin=None``.

    Args:
      command (list or str): The command to run. Typically this is a
          sequence of strings such as ``['ls', '-l', 'directory with spaces']``,
          where the first element names the executable to invoke and the other
          elements specify its arguments. With ``shell=True`` in the
          ``**options``, or on Windows, ``command`` may alternatively
          be a string, which will be parsed following platform-dependent
          :ref:`quoting rules <subprocess-quoting>`.

      stdin (:obj:`bytes`, subprocess.PIPE, file descriptor, or None): The
          bytes to provide to the subprocess on its standard input stream, or
          ``None`` if the subprocess's standard input should come from the
          same place as the parent Trio process's standard input. As is the
          case with the :mod:`subprocess` module, you can also pass a file
          descriptor or an object with a ``fileno()`` method, in which case
          the subprocess's standard input will come from that file.

          When starting `run_process` as a background task, you can also use
          ``stdin=subprocess.PIPE``, in which case `Process.stdin` will be a
          `~trio.abc.SendStream` that you can use to send data to the child.

      capture_stdout (bool): If true, capture the bytes that the subprocess
          writes to its standard output stream and return them in the
          `~subprocess.CompletedProcess.stdout` attribute of the returned
          `subprocess.CompletedProcess` or `subprocess.CalledProcessError`.

      capture_stderr (bool): If true, capture the bytes that the subprocess
          writes to its standard error stream and return them in the
          `~subprocess.CompletedProcess.stderr` attribute of the returned
          `~subprocess.CompletedProcess` or `subprocess.CalledProcessError`.

      check (bool): If false, don't validate that the subprocess exits
          successfully. You should be sure to check the
          ``returncode`` attribute of the returned object if you pass
          ``check=False``, so that errors don't pass silently.

      deliver_cancel (async function or None): If `run_process` is cancelled,
          then it needs to kill the child process. There are multiple ways to
          do this, so we let you customize it.

          If you pass None (the default), then the behavior depends on the
          platform:

          - On Windows, Trio calls ``TerminateProcess``, which should kill the
            process immediately.

          - On Unix-likes, the default behavior is to send a ``SIGTERM``, wait
            5 seconds, and send a ``SIGKILL``.

          Alternatively, you can customize this behavior by passing in an
          arbitrary async function, which will be called with the `Process`
          object as an argument. For example, the default Unix behavior could
          be implemented like this::

             async def my_deliver_cancel(process):
                 process.send_signal(signal.SIGTERM)
                 await trio.sleep(5)
                 process.send_signal(signal.SIGKILL)

          When the process actually exits, the ``deliver_cancel`` function
          will automatically be cancelled – so if the process exits after
          ``SIGTERM``, then we'll never reach the ``SIGKILL``.

          In any case, `run_process` will always wait for the child process to
          exit before raising `Cancelled`.

      **options: :func:`run_process` also accepts any :ref:`general subprocess
          options <subprocess-options>` and passes them on to the
          :class:`~trio.Process` constructor. This includes the
          ``stdout`` and ``stderr`` options, which provide additional
          redirection possibilities such as ``stderr=subprocess.STDOUT``,
          ``stdout=subprocess.DEVNULL``, or file descriptors.

    Returns:

      When called normally – a `subprocess.CompletedProcess` instance
      describing the return code and outputs.

      When called via `Nursery.start` – a `trio.Process` instance.

    Raises:
      UnicodeError: if ``stdin`` is specified as a Unicode string, rather
          than bytes
      ValueError: if multiple redirections are specified for the same
          stream, e.g., both ``capture_stdout=True`` and
          ``stdout=subprocess.DEVNULL``
      subprocess.CalledProcessError: if ``check=False`` is not passed
          and the process exits with a nonzero exit status
      OSError: if an error is encountered starting or communicating with
          the process

    .. note:: The child process runs in the same process group as the parent
       Trio process, so a Ctrl+C will be delivered simultaneously to both
       parent and child. If you don't want this behavior, consult your
       platform's documentation for starting child processes in a different
       process group.

    """

    if isinstance(stdin, str):
        raise UnicodeError("process stdin must be bytes, not str")
    if task_status is trio.TASK_STATUS_IGNORED:
        if stdin is subprocess.PIPE:
            raise ValueError(
                "stdout=subprocess.PIPE is only valid with nursery.start, "
                "since that's the only way to access the pipe; use nursery.start "
                "or pass the data you want to write directly"
            )
        if options.get("stdout") is subprocess.PIPE:
            raise ValueError(
                "stdout=subprocess.PIPE is only valid with nursery.start, "
                "since that's the only way to access the pipe"
            )
        if options.get("stderr") is subprocess.PIPE:
            raise ValueError(
                "stderr=subprocess.PIPE is only valid with nursery.start, "
                "since that's the only way to access the pipe"
            )
    if isinstance(stdin, (bytes, bytearray, memoryview)):
        input = stdin
        options["stdin"] = subprocess.PIPE
    else:
        # stdin should be something acceptable to Process
        # (None, DEVNULL, a file descriptor, etc) and Process
        # will raise if it's not
        input = None
        options["stdin"] = stdin

    if capture_stdout:
        if "stdout" in options:
            raise ValueError("can't specify both stdout and capture_stdout")
        options["stdout"] = subprocess.PIPE
    if capture_stderr:
        if "stderr" in options:
            raise ValueError("can't specify both stderr and capture_stderr")
        options["stderr"] = subprocess.PIPE

    if deliver_cancel is None:
        if os.name == "nt":
            deliver_cancel = _windows_deliver_cancel
        else:
            assert os.name == "posix"
            deliver_cancel = _posix_deliver_cancel

    stdout_chunks = []
    stderr_chunks = []

    async def feed_input(stream):
        async with stream:
            try:
                await stream.send_all(input)
            except trio.BrokenResourceError:
                pass

    async def read_output(stream, chunks):
        async with stream:
            async for chunk in stream:
                chunks.append(chunk)

    async with trio.open_nursery() as nursery:
        proc = await open_process(command, **options)
        try:
            if input is not None:
                nursery.start_soon(feed_input, proc.stdin)
                proc.stdin = None
                proc.stdio = None
            if capture_stdout:
                nursery.start_soon(read_output, proc.stdout, stdout_chunks)
                proc.stdout = None
                proc.stdio = None
            if capture_stderr:
                nursery.start_soon(read_output, proc.stderr, stderr_chunks)
                proc.stderr = None
            task_status.started(proc)
            await proc.wait()
        except BaseException:
            with trio.CancelScope(shield=True):
                killer_cscope = trio.CancelScope(shield=True)

                async def killer():
                    with killer_cscope:
                        await deliver_cancel(proc)

                nursery.start_soon(killer)
                await proc.wait()
                killer_cscope.cancel()
                raise

    stdout = b"".join(stdout_chunks) if capture_stdout else None
    stderr = b"".join(stderr_chunks) if capture_stderr else None

    if proc.returncode and check:
        raise subprocess.CalledProcessError(
            proc.returncode, proc.args, output=stdout, stderr=stderr
        )
    else:
        return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
