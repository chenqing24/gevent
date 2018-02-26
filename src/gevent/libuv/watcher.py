# pylint: disable=too-many-lines, protected-access, redefined-outer-name, not-callable
# pylint: disable=no-member
from __future__ import absolute_import, print_function

import functools
import sys

import gevent.libuv._corecffi as _corecffi # pylint:disable=no-name-in-module,import-error

ffi = _corecffi.ffi
libuv = _corecffi.lib

from gevent._ffi import watcher as _base
from gevent._ffi import _dbg

_closing_handles = set()


@ffi.def_extern()
def _uv_close_callback(handle):
    _closing_handles.remove(handle)


_events = [(libuv.UV_READABLE, "READ"),
           (libuv.UV_WRITABLE, "WRITE")]

def _events_to_str(events): # export
    return _base.events_to_str(events, _events)

class UVFuncallError(ValueError):
    pass

class libuv_error_wrapper(object):
    # Makes sure that everything stored as a function
    # on the wrapper instances (classes, actually,
    # because this is used by the metaclass)
    # checks its return value and raises an error.
    # This expects that everything we call has an int
    # or void return value and follows the conventions
    # of error handling (that negative values are errors)
    def __init__(self, uv):
        self._libuv = uv

    def __getattr__(self, name):
        libuv_func = getattr(self._libuv, name)

        @functools.wraps(libuv_func)
        def wrap(*args, **kwargs):
            if args and isinstance(args[0], watcher):
                args = args[1:]
            res = libuv_func(*args, **kwargs)
            if res is not None and res < 0:
                raise UVFuncallError(
                    str(ffi.string(libuv.uv_err_name(res)).decode('ascii')
                        + ' '
                        + ffi.string(libuv.uv_strerror(res)).decode('ascii'))
                    + " Args: " + repr(args) + " KWARGS: " + repr(kwargs)
                )
            return res

        setattr(self, name, wrap)

        return wrap


class ffi_unwrapper(object):
    # undoes the wrapping of libuv_error_wrapper for
    # the methods used by the metaclass that care

    def __init__(self, ff):
        self._ffi = ff

    def __getattr__(self, name):
        return getattr(self._ffi, name)

    def addressof(self, lib, name):
        assert isinstance(lib, libuv_error_wrapper)
        return self._ffi.addressof(libuv, name)


class watcher(_base.watcher):
    _FFI = ffi_unwrapper(ffi)
    _LIB = libuv_error_wrapper(libuv)

    _watcher_prefix = 'uv'
    _watcher_struct_pattern = '%s_t'
    _watcher_callback_name = '_gevent_generic_callback0'


    @classmethod
    def _watcher_ffi_close(cls, ffi_watcher):
        # Managing the lifetime of _watcher is tricky.
        # They have to be uv_close()'d, but that only
        # queues them to be closed in the *next* loop iteration.
        # The memory must stay valid for at least that long,
        # or assert errors are triggered. We can't use a ffi.gc()
        # pointer to queue the uv_close, because by the time the
        # destructor is called, there's no way to keep the memory alive
        # and it could be re-used.
        # So here we resort to resurrecting the pointer object out
        # of our scope, keeping it alive past this object's lifetime.
        # We then use the uv_close callback to handle removing that
        # reference. There's no context passed to the close callback,
        # so we have to do this globally.

        # Sadly, doing this causes crashes if there were multiple
        # watchers for a given FD, so we have to take special care
        # about that. See https://github.com/gevent/gevent/issues/790#issuecomment-208076604

        # Note that this cannot be a __del__ method, because we store
        # the CFFI handle to self on self, which is a cycle, and
        # objects with a __del__ method cannot be collected on CPython < 3.4

        # Instead, this is arranged as a callback to GC when the
        # watcher class dies. Obviously it's important to keep the ffi
        # watcher alive.
        _dbg("Request to close handle", ffi_watcher)
        # We can pass in "subclasses" if uv_handle_t that line up at the C level,
        # but that don't in CFFI without a cast. But be careful what we use the cast
        # for, don't pass it back to C.
        ffi_handle_watcher = cls._FFI.cast('uv_handle_t*', ffi_watcher)
        if ffi_handle_watcher.type and not libuv.uv_is_closing(ffi_watcher):
            # If the type isn't set, we were never properly initialized,
            # and trying to close it results in libuv terminating the process.
            # Sigh. Same thing if it's already in the process of being
            # closed.
            _dbg("Closing handle", ffi_watcher)
            _closing_handles.add(ffi_watcher)
            libuv.uv_close(ffi_watcher, libuv._uv_close_callback)

        ffi_handle_watcher.data = ffi.NULL


    def _watcher_ffi_set_init_ref(self, ref):
        _dbg("Creating", type(self), "with ref", ref)
        self.ref = ref

    def _watcher_ffi_init(self, args):
        # TODO: we could do a better job chokepointing this
        return self._watcher_init(self.loop.ptr,
                                  self._watcher,
                                  *args)

    def _watcher_ffi_start(self):
        _dbg("Starting", self)
        self._watcher_start(self._watcher, self._watcher_callback)
        _dbg("\tStarted", self)

    def _watcher_ffi_stop(self):
        _dbg("Stopping", self, self._watcher_stop)
        if self._watcher:
            # The multiplexed io watcher deletes self._watcher
            # when it closes down. If that's in the process of
            # an error handler, AbstractCallbacks.unhandled_onerror
            # will try to close us again.
            self._watcher_stop(self._watcher)
        _dbg("Stopped", self)

    @_base.only_if_watcher
    def _watcher_ffi_ref(self):
        _dbg("Reffing", self)
        libuv.uv_ref(self._watcher)

    @_base.only_if_watcher
    def _watcher_ffi_unref(self):
        _dbg("Unreffing", self)
        libuv.uv_unref(self._watcher)

    def _watcher_ffi_start_unref(self):
        pass

    def _watcher_ffi_stop_ref(self):
        pass

    def _get_ref(self):
        # Convert 1/0 to True/False
        if self._watcher is None:
            return None
        return True if libuv.uv_has_ref(self._watcher) else False

    def _set_ref(self, value):
        if value:
            self._watcher_ffi_ref()
        else:
            self._watcher_ffi_unref()

    ref = property(_get_ref, _set_ref)

    def feed(self, _revents, _callback, *_args):
        raise Exception("Not implemented")

class io(_base.IoMixin, watcher):
    _watcher_type = 'poll'
    _watcher_callback_name = '_gevent_poll_callback2'

    # On Windows is critical to be able to garbage collect these
    # objects in a timely fashion so that they don't get reused
    # for multiplexing completely different sockets. This is because
    # uv_poll_init_socket does a lot of setup for the socket to make
    # polling work. If get reused for another socket that has the same
    # fileno, things break badly. (In theory this could be a problem
    # on posix too, but in practice it isn't).

    # TODO: We should probably generalize this to all
    # ffi watchers. Avoiding GC cycles as much as possible
    # is a good thing, and potentially allocating new handles
    # as needed gets us better memory locality.

    # Especially on Windows, we must also account for the case that a
    # reference to this object has leaked (e.g., the socket object is
    # still around), but the fileno has been closed and a new one
    # opened. We must still get a new native watcher at that point. We
    # handle this case by simply making sure that we don't even have
    # a native watcher until the object is started, and we shut it down
    # when the object is stopped.

    # XXX: I was able to solve at least Windows test_ftplib.py issues
    # with more of a careful use of io objects in socket.py, so
    # delaying this entirely is at least temporarily on hold. Instead
    # sticking with the _watcher_create function override for the
    # moment.

    # XXX: Note 2: Moving to a deterministic close model, which was necessary
    # for PyPy, also seems to solve the Windows issues. So we're completely taking
    # this object out of the loop's registration; we don't want GC callbacks and
    # uv_close anywhere *near* this object.

    _watcher_registers_with_loop_on_create = False

    EVENT_MASK = libuv.UV_READABLE | libuv.UV_WRITABLE | libuv.UV_DISCONNECT

    _multiplex_watchers = ()

    def __init__(self, loop, fd, events, ref=True, priority=None):
        super(io, self).__init__(loop, fd, events, ref=ref, priority=priority, _args=(fd,))
        self._fd = fd
        self._events = events
        self._multiplex_watchers = []

    def _get_fd(self):
        return self._fd

    @_base.not_while_active
    def _set_fd(self, fd):
        self._fd = fd
        self._watcher_ffi_init((fd,))

    def _get_events(self):
        return self._events

    def _set_events(self, events):
        if events == self._events:
            return
        _dbg("Changing event mask for", self, "from", self._events, "to", events)
        self._events = events
        if self.active:
            # We're running but libuv specifically says we can
            # call start again to change our event mask.
            assert self._handle is not None
            self._watcher_start(self._watcher, self._events, self._watcher_callback)

    def _watcher_ffi_start(self):
        _dbg("Starting watcher", self, "with events", self._events)
        self._watcher_start(self._watcher, self._events, self._watcher_callback)

    if sys.platform.startswith('win32'):
        # uv_poll can only handle sockets on Windows, but the plain
        # uv_poll_init we call on POSIX assumes that the fileno
        # argument is already a C fileno, as created by
        # _get_osfhandle. C filenos are limited resources, must be
        # closed with _close. So there are lifetime issues with that:
        # calling the C function _close to dispose of the fileno
        # *also* closes the underlying win32 handle, possibly
        # prematurely. (XXX: Maybe could do something with weak
        # references? But to what?)

        # All libuv wants to do with the fileno in uv_poll_init is
        # turn it back into a Win32 SOCKET handle.

        # Now, libuv provides uv_poll_init_socket, which instead of
        # taking a C fileno takes the SOCKET, avoiding the need to dance with
        # the C runtime.

        # It turns out that SOCKET (win32 handles in general) can be
        # represented with `intptr_t`. It further turns out that
        # CPython *directly* exposes the SOCKET handle as the value of
        # fileno (32-bit PyPy does some munging on it, which should
        # rarely matter). So we can pass socket.fileno() through
        # to uv_poll_init_socket.

        # See _corecffi_build.
        _watcher_init = watcher._LIB.uv_poll_init_socket


    class _multiplexwatcher(object):

        callback = None
        args = ()
        pass_events = False
        ref = True

        def __init__(self, events, watcher):
            self._events = events

            # References:
            # These objects must keep the original IO object alive;
            # the IO object SHOULD NOT keep these alive to avoid cycles
            # We MUST NOT rely on GC to clean up the IO objects, but the explicit
            # calls to close(); see _multiplex_closed.
            self._watcher_ref = watcher

        events = property(
            lambda self: self._events,
            _base.not_while_active(lambda self, nv: setattr(self, '_events', nv)))

        def start(self, callback, *args, **kwargs):
            _dbg("Starting IO multiplex watcher for", self.fd,
                 "callback", callback, "events", self.events,
                 "owner", self._watcher_ref)
            self.pass_events = kwargs.get("pass_events")
            self.callback = callback
            self.args = args

            watcher = self._watcher_ref
            if watcher is not None and not watcher.active:
                watcher._io_start()

        def stop(self):
            _dbg("Stopping IO multiplex watcher for", self.fd,
                 "callback", self.callback, "events", self.events,
                 "owner", self._watcher_ref)
            self.callback = None
            self.pass_events = None
            self.args = None
            watcher = self._watcher_ref
            if watcher is not None:
                watcher._io_maybe_stop()

        def close(self):
            if self._watcher_ref is not None:
                self._watcher_ref._multiplex_closed(self)
            self._watcher_ref = None

        @property
        def active(self):
            return self.callback is not None

        @property
        def _watcher(self):
            # For testing.
            return self._watcher_ref._watcher

        # ares.pyx depends on this property,
        # and test__core uses it too
        fd = property(lambda self: getattr(self._watcher_ref, '_fd', -1),
                      lambda self, nv: self._watcher_ref._set_fd(nv))

    def _io_maybe_stop(self):
        for w in self._multiplex_watchers:
            if w.callback is not None:
                # There's still a reference to it, and it's started,
                # so we can't stop.
                return
        # If we get here, nothing was started
        # so we can take ourself out of the polling set
        self.stop()

    def _io_start(self):
        _dbg("IO start on behalf of multiplex", self, "fd", self._fd, "events", self._events)
        self.start(self._io_callback, pass_events=True)

    def _calc_and_update_events(self):
        events = 0
        for watcher in self._multiplex_watchers:
            events |= watcher.events
        self._set_events(events)


    def multiplex(self, events):
        watcher = self._multiplexwatcher(events, self)
        self._multiplex_watchers.append(watcher)
        self._calc_and_update_events()
        return watcher

    def close(self):
        super(io, self).close()
        del self._multiplex_watchers

    def _multiplex_closed(self, watcher):
        self._multiplex_watchers.remove(watcher)
        if not self._multiplex_watchers:
            _dbg("IO Watcher", self, "has no more multiplexes")
            self.stop() # should already be stopped
            self._no_more_watchers()
            # It is absolutely critical that we control when the call
            # to uv_close() gets made. uv_close() of a uv_poll_t
            # handle winds up calling uv__platform_invalidate_fd,
            # which, as the name implies, destroys any outstanding
            # events for the *fd* that haven't been delivered yet, and also removes
            # the *fd* from the poll set. So if this happens later, at some
            # non-deterministic time when (cyclic or otherwise) GC runs,
            # *and* we've opened a new watcher for the fd, that watcher will
            # suddenly and mysteriously stop seeing events. So we do this now;
            # this method is smart enough not to close the handle twice.
            self.close()
        else:
            self._calc_and_update_events()
            _dbg("IO Watcher", self, "has remaining multiplex:",
                 self._multiplex_watchers)

    def _no_more_watchers(self):
        # The loop sets this on an individual watcher to delete it from
        # the active list where it keeps hard references.
        pass

    def _io_callback(self, events):
        if events < 0:
            # actually a status error code
            _dbg("Callback error on", self._fd,
                 ffi.string(libuv.uv_err_name(events)),
                 ffi.string(libuv.uv_strerror(events)))
            # XXX: We've seen one half of a FileObjectPosix pair
            # (the read side of a pipe) report errno 11 'bad file descriptor'
            # after the write side was closed and its watcher removed. But
            # we still need to attempt to read from it to clear out what's in
            # its buffers--if we return with the watcher inactive before proceeding to wake up
            # the reader, we get a LoopExit. So we can't return here and arguably shouldn't print it
            # either. The negative events mask will match the watcher's mask.
            # See test__fileobject.py:Test.test_newlines for an example.

            # On Windows (at least with PyPy), we can get ENOTSOCK (socket operation on non-socket)
            # if a socket gets closed. If we don't pass the events on, we hang.
            # See test__makefile_ref.TestSSL for examples.
            # return

        _dbg("Callback event for watcher", self._fd, "event", events)
        for watcher in self._multiplex_watchers:
            if not watcher.callback:
                # Stopped
                _dbg("Watcher", self, "has stopped multiplex", watcher)
                continue
            assert watcher._watcher_ref is self, (self, watcher._watcher_ref)
            _dbg("Event for watcher", self._fd, events, watcher.events, events & watcher.events)

            send_event = (events & watcher.events) or events < 0
            if send_event:
                if not watcher.pass_events:
                    watcher.callback(*watcher.args)
                else:
                    watcher.callback(events, *watcher.args)

class _SimulatedWithAsyncMixin(object):
    _watcher_skip_ffi = True

    def __init__(self, loop, *args, **kwargs):
        self._async = loop.async_()
        try:
            super(_SimulatedWithAsyncMixin, self).__init__(loop, *args, **kwargs)
        except:
            self._async.close()
            raise

    def _watcher_create(self, _args):
        return

    @property
    def _watcher_handle(self):
        return None

    def _watcher_ffi_init(self, _args):
        return

    def _watcher_ffi_set_init_ref(self, ref):
        self._async.ref = ref

    @property
    def active(self):
        return self._async.active

    def start(self, cb, *args):
        self._register_loop_callback()
        self.callback = cb
        self.args = args
        self._async.start(cb, *args)
        #watcher.start(self, cb, *args)

    def stop(self):
        self._unregister_loop_callback()
        self.callback = None
        self.args = None
        self._async.stop()

    def close(self):
        if self._async is not None:
            a = self._async
            #self._async = None
            a.close()

    def _register_loop_callback(self):
        # called from start()
        raise NotImplementedError()

    def _unregister_loop_callback(self):
        # called from stop
        raise NotImplementedError()

class fork(_SimulatedWithAsyncMixin,
           _base.ForkMixin,
           watcher):
    # We'll have to implement this one completely manually
    # Right now it doesn't matter much since libuv doesn't survive
    # a fork anyway. (That's a work in progress)
    _watcher_skip_ffi = False

    def _register_loop_callback(self):
        self.loop._fork_watchers.add(self)

    def _unregister_loop_callback(self):
        try:
            # stop() should be idempotent
            self.loop._fork_watchers.remove(self)
        except KeyError:
            pass

    def _on_fork(self):
        self._async.send()


class child(_SimulatedWithAsyncMixin,
            _base.ChildMixin,
            watcher):
    _watcher_skip_ffi = True
    # We'll have to implement this one completely manually.
    # Our approach is to use a SIGCHLD handler and the original
    # os.waitpid call.

    # On Unix, libuv's uv_process_t and uv_spawn use SIGCHLD,
    # just like libev does for its child watchers. So
    # we're not adding any new SIGCHLD related issues not already
    # present in libev.


    def _register_loop_callback(self):
        self.loop._register_child_watcher(self)

    def _unregister_loop_callback(self):
        self.loop._unregister_child_watcher(self)

    def _set_waitpid_status(self, pid, status):
        self._rpid = pid
        self._rstatus = status
        self._async.send()


class async_(_base.AsyncMixin, watcher):

    def _watcher_ffi_init(self, args):
        # It's dangerous to have a raw, non-initted struct
        # around; it will crash in uv_close() when we get GC'd,
        # and send() will also crash.
        # NOTE: uv_async_init is NOT idempotent. Calling it more than
        # once adds the uv_async_t to the internal queue multiple times,
        # and uv_close only cleans up one of them, meaning that we tend to
        # crash. Thus we have to be very careful not to allow that.
        return self._watcher_init(self.loop.ptr, self._watcher, ffi.NULL)

    def _watcher_ffi_start(self):
        # we're created in a started state, but we didn't provide a
        # callback (because if we did and we don't have a value in our
        # callback attribute, then python_callback would crash.) Note that
        # uv_async_t->async_cb is not technically documented as public.
        self._watcher.async_cb = self._watcher_callback

    def _watcher_ffi_stop(self):
        self._watcher.async_cb = ffi.NULL
        # We have to unref this because we're setting the cb behind libuv's
        # back, basically: once a async watcher is started, it can't ever be
        # stopped through libuv interfaces, so it would never lose its active
        # status, and thus if it stays reffed it would keep the event loop
        # from exiting.
        self._watcher_ffi_unref()

    def send(self):
        if libuv.uv_is_closing(self._watcher):
            raise Exception("Closing handle")
        libuv.uv_async_send(self._watcher)

    @property
    def pending(self):
        return None

locals()['async'] = async_

class timer(_base.TimerMixin, watcher):

    # In libuv, timer callbacks continue running while any timer is
    # expired, including newly added timers. Newly added non-zero
    # timers (especially of small duration) can be seen to be expired
    # if the loop time is updated while we are in a timer callback.
    # This can lead to us being stuck running timers for a terribly
    # long time, which is not good. So default to not updating the
    # time.

    # Also, newly-added timers of 0 duration can *also* stall the
    # loop, because they'll be seen to be expired immediately.
    # Updating the time can prevent that, *if* there was already a
    # timer for a longer duration scheduled.

    # To mitigate the above problems, our loop implementation turns
    # zero duration timers into check watchers instead using OneShotCheck.
    # This ensures the loop cycles. Of course, the 'again' method does
    # nothing on them and doesn't exist. In practice that's not an issue.

    _again = False

    def _watcher_ffi_init(self, args):
        self._watcher_init(self.loop._ptr, self._watcher)
        self._after, self._repeat = args
        if self._after and self._after < 0.001:
            import warnings
            warnings.warn("libuv only supports millisecond timer resolution; "
                          "all times less will be set to 1 ms",
                          stacklevel=6)
            # The alternative is to effectively pass in int(0.1) == 0, which
            # means no sleep at all, which leads to excessive wakeups
            self._after = 0.001
        if self._repeat and self._repeat < 0.001:
            import warnings
            warnings.warn("libuv only supports millisecond timer resolution; "
                          "all times less will be set to 1 ms",
                          stacklevel=6)
            self._repeat = 0.001

    def _watcher_ffi_start(self):
        if self._again:
            libuv.uv_timer_again(self._watcher)
        else:
            try:
                self._watcher_start(self._watcher, self._watcher_callback,
                                    int(self._after * 1000),
                                    int(self._repeat * 1000))
            except ValueError:
                # in case of non-ints in _after/_repeat
                raise TypeError()

    def again(self, callback, *args, **kw):
        if not self.active:
            # If we've never been started, this is the same as starting us.
            # libuv makes the distinction, libev doesn't.
            self.start(callback, *args, **kw)
            return

        self._again = True
        try:
            self.start(callback, *args, **kw)
        finally:
            del self._again


class stat(_base.StatMixin, watcher):
    _watcher_type = 'fs_poll'
    _watcher_struct_name = 'gevent_fs_poll_t'
    _watcher_callback_name = '_gevent_fs_poll_callback3'

    def _watcher_set_data(self, the_watcher, data):
        the_watcher.handle.data = data
        return data

    def _watcher_ffi_init(self, args):
        return self._watcher_init(self.loop._ptr, self._watcher)

    MIN_STAT_INTERVAL = 0.1074891 # match libev; 0.0 is default

    def _watcher_ffi_start(self):
        # libev changes this when the watcher is started
        if self._interval < self.MIN_STAT_INTERVAL:
            self._interval = self.MIN_STAT_INTERVAL
        self._watcher_start(self._watcher, self._watcher_callback,
                            self._cpath,
                            int(self._interval * 1000))

    @property
    def _watcher_handle(self):
        return self._watcher.handle.data

    @property
    def attr(self):
        if not self._watcher.curr.st_nlink:
            return
        return self._watcher.curr

    @property
    def prev(self):
        if not self._watcher.prev.st_nlink:
            return
        return self._watcher.prev


class signal(_base.SignalMixin, watcher):

    _watcher_callback_name = '_gevent_generic_callback1'

    def _watcher_ffi_init(self, args):
        self._watcher_init(self.loop._ptr, self._watcher)
        self.ref = False # libev doesn't ref these by default


    def _watcher_ffi_start(self):
        self._watcher_start(self._watcher, self._watcher_callback,
                            self._signalnum)


class idle(_base.IdleMixin, watcher):
    # Because libuv doesn't support priorities, idle watchers are
    # potentially quite a bit different than under libev
    pass


class check(_base.CheckMixin, watcher):
    pass

class OneShotCheck(check):

    _watcher_skip_ffi = True

    def start(self, callback, *args):
        @functools.wraps(callback)
        def _callback(*args):
            self.stop()
            return callback(*args)
        return check.start(self, _callback, *args)

class prepare(_base.PrepareMixin, watcher):
    pass