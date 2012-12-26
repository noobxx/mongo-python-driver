# Copyright 2011-2012 10gen, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Motor, an asynchronous driver for MongoDB and Tornado."""

import functools
import socket
import time
import warnings

# So that 'setup.py doc' can import this module without Tornado or greenlet
requirements_satisfied = True
try:
    from tornado import ioloop, iostream, gen
except ImportError:
    requirements_satisfied = False
    warnings.warn("Tornado not installed", ImportWarning)

try:
    import greenlet
except ImportError:
    requirements_satisfied = False
    warnings.warn("greenlet module not installed", ImportWarning)


import pymongo
import pymongo.common
import pymongo.errors
import pymongo.mongo_client
import pymongo.mongo_replica_set_client
import pymongo.pool
import pymongo.son_manipulator
import gridfs

from pymongo.database import Database
from pymongo.collection import Collection
from pymongo.cursor import Cursor
from gridfs import grid_file


__all__ = [
    'MotorClient', 'MotorReplicaSetClient', 'Op', 'WaitOp',
    'WaitAllOps'
]

# TODO: ensure we're doing
#   timeouts as efficiently as possible, test performance hit with timeouts
#   from registering and cancelling timeouts
# TODO: test cross-host copydb
# TODO: perhaps remove versionchanged Sphinx annotations from proxied methods,
#   unless versionchanged >= 2.3 or so -- whenever Motor joins PyMongo
# TODO: review open_sync(), does it need to disconnect after success to ensure
#   all IOStreams with old IOLoop are gone?
# TODO: what do safe=True and other get_last_error options mean when creating
#   a MotorClient or MotorReplicaSetClient?
# TODO: ensure all documented methods return MotorGridOut, not GridOut,
#   MotorCollection, not Collection, etc.


def check_callable(kallable, required=False):
    if required and not kallable:
        raise TypeError("callable is required")
    if kallable is not None and not callable(kallable):
        raise TypeError("callback must be callable")


def motor_sock_method(method):
    """Wrap a MotorSocket method to pause the current greenlet and arrange
       for the greenlet to be resumed when non-blocking I/O has completed.
    """
    @functools.wraps(method)
    def _motor_sock_method(self, *args, **kwargs):
        child_gr = greenlet.getcurrent()
        main = child_gr.parent
        assert main, "Should be on child greenlet"

        timeout_object = None

        if self.timeout:
            def timeout_err():
                # Running on the main greenlet. If a timeout error is thrown,
                # we raise the exception on the child greenlet. Closing the
                # IOStream removes callback() from the IOLoop so it isn't
                # called.
                self.stream.set_close_callback(None)
                self.stream.close()
                child_gr.throw(socket.timeout("timed out"))

            timeout_object = self.stream.io_loop.add_timeout(
                time.time() + self.timeout, timeout_err)

        # This is run by IOLoop on the main greenlet when operation
        # completes; switch back to child to continue processing
        def callback(result=None):
            self.stream.set_close_callback(None)
            if timeout_object:
                self.stream.io_loop.remove_timeout(timeout_object)

            child_gr.switch(result)

        # Run on main greenlet
        def closed():
            if timeout_object:
                self.stream.io_loop.remove_timeout(timeout_object)

            # The child greenlet might have died, e.g.:
            # - An operation raised an error within PyMongo
            # - PyMongo closed the MotorSocket in response
            # - MotorSocket.close() closed the IOStream
            # - IOStream scheduled this closed() function on the loop
            # - PyMongo operation completed (with or without error) and
            #       its greenlet terminated
            # - IOLoop runs this function
            if not child_gr.dead:
                child_gr.throw(socket.error("error"))

        self.stream.set_close_callback(closed)

        try:
            kwargs['callback'] = callback

            # method is MotorSocket.send(), recv(), etc. method() begins a
            # non-blocking operation on an IOStream and arranges for
            # callback() to be executed on the main greenlet once the
            # operation has completed.
            method(self, *args, **kwargs)

            # Pause child greenlet until resumed by main greenlet, which
            # will pass the result of the socket operation (data for recv,
            # number of bytes written for sendall) to us.
            socket_result = main.switch()
            return socket_result
        except socket.error:
            raise
        except IOError, e:
            # If IOStream raises generic IOError (e.g., if operation
            # attempted on closed IOStream), then substitute socket.error,
            # since socket.error is what PyMongo's built to handle. For
            # example, PyMongo will catch socket.error, close the socket,
            # and raise AutoReconnect.
            raise socket.error(str(e))

    return _motor_sock_method


class MotorSocket(object):
    """Replace socket with a class that yields from the current greenlet, if
    we're on a child greenlet, when making blocking calls, and uses Tornado
    IOLoop to schedule child greenlet for resumption when I/O is ready.

    We only implement those socket methods actually used by pymongo.
    """
    def __init__(self, sock, io_loop, use_ssl=False):
        self.use_ssl = use_ssl
        self.timeout = None
        if self.use_ssl:
           self.stream = iostream.SSLIOStream(sock, io_loop=io_loop)
        else:
           self.stream = iostream.IOStream(sock, io_loop=io_loop)

    def setsockopt(self, *args, **kwargs):
        self.stream.socket.setsockopt(*args, **kwargs)

    def settimeout(self, timeout):
        # IOStream calls socket.setblocking(False), which does settimeout(0.0).
        # We must not allow pymongo to set timeout to some other value (a
        # positive number or None) or the socket will start blocking again.
        # Instead, we simulate timeouts by interrupting ourselves with
        # callbacks.
        self.timeout = timeout

    @motor_sock_method
    def connect(self, pair, callback):
        """
        :Parameters:
         - `pair`: A tuple, (host, port)
        """
        self.stream.connect(pair, callback)

    @motor_sock_method
    def sendall(self, data, callback):
        self.stream.write(data, callback)

    @motor_sock_method
    def recv(self, num_bytes, callback):
        self.stream.read_bytes(num_bytes, callback)

    def close(self):
        # TODO: examine this, decide if it's correct, and if so explain why
        self.stream.set_close_callback(None)
        
        sock = self.stream.socket
        try:
            try:
                self.stream.close()
            except KeyError:
                # Tornado's _impl (epoll, kqueue, ...) has already removed this
                # file descriptor from its dict.
                pass
        finally:
            # Sometimes necessary to avoid ResourceWarnings in Python 3:
            # specifically, if the fd is closed from the OS's view, then
            # stream.close() throws an exception, but the socket still has an
            # fd and so will print a ResourceWarning. In that case, calling
            # sock.close() directly clears the fd and does not raise an error.
            if sock:
                sock.close()

    def fileno(self):
        return self.stream.socket.fileno()


class MotorPool(pymongo.pool.GreenletPool):
    """A simple connection pool of MotorSockets.

    Note this inherits from GreenletPool so that when PyMongo internally calls
    start_request, e.g. in Database.authenticate() or
    MongoClient.copy_database(), this pool assigns a socket to the current
    greenlet for the duration of the method. Request semantics are not exposed
    to Motor's users.
    """
    def __init__(self, io_loop, *args, **kwargs):
        self.io_loop = io_loop
        pymongo.pool.GreenletPool.__init__(self, *args, **kwargs)

    def create_connection(self, pair):
        """Copy of BasePool.connect()
        """
        # TODO: refactor all this with BasePool, use a new hook to wrap the
        #   socket with MotorSocket before attempting connect().
        assert greenlet.getcurrent().parent, "Should be on child greenlet"

        host, port = pair or self.pair

        # Don't try IPv6 if we don't support it. Also skip it if host
        # is 'localhost' (::1 is fine). Avoids slow connect issues
        # like PYTHON-356.
        family = socket.AF_INET
        if socket.has_ipv6 and host != 'localhost':
            family = socket.AF_UNSPEC

        err = None
        for res in socket.getaddrinfo(host, port, family, socket.SOCK_STREAM):
            af, socktype, proto, dummy, sa = res
            motor_sock = None
            try:
                sock = socket.socket(af, socktype, proto)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                motor_sock = MotorSocket(
                    sock, self.io_loop, use_ssl=self.use_ssl)
                motor_sock.settimeout(self.conn_timeout or 20.0)

                # MotorSocket will pause the current greenlet and resume it
                # when connection has completed
                motor_sock.connect(pair or self.pair)
                return motor_sock
            except socket.error, e:
                err = e
                if motor_sock is not None:
                    motor_sock.close()

        if err is not None:
            raise err
        else:
            # This likely means we tried to connect to an IPv6 only
            # host with an OS/kernel or Python interpeter that doesn't
            # support IPv6.
            raise socket.error('getaddrinfo failed')

    def connect(self, pair):
        """Copy of BasePool.connect(), avoiding call to ssl.wrap_socket which
           is inappropriate for Motor.
           TODO: refactor, extra hooks in BasePool
        """
        motor_sock = self.create_connection(pair)
        motor_sock.settimeout(self.net_timeout)
        return pymongo.pool.SocketInfo(motor_sock, self.pool_id)


def asynchronize(io_loop, sync_method, has_safe_arg, callback_required):
    """
    Decorate `sync_method` so it's run on a child greenlet and executes
    `callback` with (result, error) arguments when greenlet completes.

    :Parameters:
     - `io_loop`:           A Tornado IOLoop
     - `sync_method`:       Bound method of pymongo Collection, Database,
                            MongoClient, or Cursor
     - `has_safe_arg`:      Whether the method takes a 'safe' argument
     - `callback_required`: If True, raise TypeError if no callback is passed
    """
    assert isinstance(io_loop, ioloop.IOLoop), (
        "First argument to asynchronize must be IOLoop, not %s" % repr(io_loop))

    @functools.wraps(sync_method)
    def method(*args, **kwargs):
        callback = kwargs.pop('callback', None)
        check_callable(callback, required=callback_required)

        # Safe writes if callback is passed and safe=False not passed explicitly
        if 'safe' not in kwargs and has_safe_arg:
            kwargs['safe'] = bool(callback)

        def call_method():
            result, error = None, None
            try:
                result = sync_method(*args, **kwargs)
            except Exception, e:
                error = e

            # Schedule the callback to be run on the main greenlet
            if callback:
                io_loop.add_callback(
                    functools.partial(callback, result, error)
                )
            elif error:
                raise error

        # Start running the operation on a greenlet
        greenlet.greenlet(call_method).switch()

    return method


class DelegateProperty(object):
    def __init__(self, name=None):
        self.name = name

    def set_name(self, name):
        if not self.name:
            self.name = name

    def get_name(self):
        return self.name

    def wrap(self, original_class):
        return Wrap(self, original_class)

    def unwrap(self, motor_class):
        return Unwrap(self, motor_class)


class WrapBase(DelegateProperty):
    def __init__(self, prop):
        """Wraps a DelegateProperty for further processing"""
        DelegateProperty.__init__(self, prop)
        self.prop = prop

    def set_name(self, name):
        self.prop.set_name(name)

    def get_name(self):
        return self.prop.name


class Wrap(WrapBase):
    def __init__(self, prop, original_class):
        """
        TODO: UPDATE doc
        A descriptor that wraps a Motor method and wraps its return value in a
        Motor class. E.g., wrap Motor's map_reduce to return a MotorCollection
        instead of a PyMongo Collection. Calls the wrap() method on the owner
        object to do the actual wrapping at invocation time.
        """
        WrapBase.__init__(self, prop)
        self.prop = prop
        self.original_class = original_class

    def __get__(self, obj, objtype):
        f = self.prop.__get__(obj, objtype)
        original_class = self.original_class

        @functools.wraps(f)
        def _f(*args, **kwargs):
            result = f(*args, **kwargs)
            # Don't call isinstance(), not checking subclasses
            if result.__class__ is original_class:
                # Delegate to the current object to wrap the result
                return obj.wrap(result)
            return result

        return _f


class Unwrap(WrapBase):
    def __init__(self, prop, motor_class):
        """
        TODO: UPDATE doc
        A descriptor that wraps a Motor method and unwraps its arguments. E.g.,
        wrap Motor's drop_database and if a MotorDatabase is passed in, unwrap
        it and pass in a pymongo.database.Database instead.
        """
        WrapBase.__init__(self, prop)
        self.prop = prop
        self.motor_class = motor_class

    def __get__(self, obj, objtype):
        f = self.prop.__get__(obj, objtype)
        if isinstance(self.motor_class, basestring):
            # Delayed reference - e.g., drop_database is defined before
            # MotorDatabase is, so it was initialized with
            # unwrap('MotorDatabase') instead of unwrap(MotorDatabase).
            motor_class = globals()[self.motor_class]
        else:
            motor_class = self.motor_class

        @functools.wraps(f)
        def _f(*args, **kwargs):
            def _unwrap_obj(obj):
                # Don't call isinstance(), not checking subclasses
                if obj.__class__ is motor_class:
                    return obj.delegate
                else:
                    return obj

            # Call _unwrap_obj on each arg and kwarg before invoking f
            args = [_unwrap_obj(arg) for arg in args]
            kwargs = dict([
                (key, _unwrap_obj(value)) for key, value in kwargs.items()])
            return f(*args, **kwargs)

        return _f


class Async(DelegateProperty):
    def __init__(self, has_safe_arg, cb_required, name=None):
        """
        A descriptor that wraps a PyMongo method, such as insert or remove, and
        returns an asynchronous version of the method, which takes a callback.

        :Parameters:
         - `has_safe_arg`:    Whether the method takes a 'safe' argument
         - `cb_required`:     Whether callback is required or optional
         - `name`:            (optional) Name of wrapped method
        """
        DelegateProperty.__init__(self, name)
        self.has_safe_arg = has_safe_arg
        self.cb_required = cb_required

    def __get__(self, obj, objtype):
        sync_method = getattr(obj.delegate, self.name)
        return asynchronize(
            obj.get_io_loop(),
            sync_method,
            has_safe_arg=self.has_safe_arg,
            callback_required=self.cb_required)

    def get_cb_required(self):
        return self.cb_required

    def wrap(self, original_class):
        return WrapAsync(self, original_class)

    def unwrap(self, motor_class):
        return UnwrapAsync(self, motor_class)


class WrapAsync(Async):
    def __init__(self, prop, original_class):
        """
        TODO: UPDATE doc
        A descriptor that wraps a Motor method and wraps its return value in a
        Motor class. E.g., wrap Motor's map_reduce to return a MotorCollection
        instead of a PyMongo Collection. Calls the wrap() method on the owner
        object to do the actual wrapping at invocation time.
        """
        Async.__init__(self, prop.has_safe_arg, prop.cb_required)
        self.prop = prop
        self.original_class = original_class

    def __get__(self, obj, objtype):
        f = self.prop.__get__(obj, objtype)
        original_class = self.original_class

        @functools.wraps(f)
        def _f(*args, **kwargs):
            callback = kwargs.pop('callback', None)
            check_callable(callback, self.prop.cb_required)
            if callback:
                def _callback(result, error):
                    if error:
                        callback(None, error)
                        return

                    # Don't call isinstance(), not checking subclasses
                    if result.__class__ is original_class:
                        # Delegate to the current object to wrap the result
                        new_object = obj.wrap(result)
                    else:
                        new_object = result

                    callback(new_object, None)
                kwargs['callback'] = _callback
            f(*args, **kwargs)
        return _f

    def set_name(self, name):
        self.prop.set_name(name)

    def get_name(self):
        return self.prop.name


class UnwrapAsync(Unwrap):
    def __init__(self, prop, motor_class):
        Unwrap.__init__(self, prop, motor_class)


class AsyncRead(Async):
    def __init__(self, name=None):
        """
        A descriptor that wraps a PyMongo read method like find_one() that
        requires a callback.
        """
        Async.__init__(self, has_safe_arg=False, cb_required=True, name=name)


class AsyncWrite(Async):
    def __init__(self, name=None):
        """
        A descriptor that wraps a PyMongo write method like update() that
        accepts optional safe and callback arguments.
        """
        Async.__init__(self, has_safe_arg=True, cb_required=False, name=name)


class AsyncCommand(Async):
    def __init__(self, name=None):
        """
        A descriptor that wraps a PyMongo command like copy_database()
        """
        Async.__init__(self, has_safe_arg=False, cb_required=False, name=name)


class ReadOnlyDelegateProperty(DelegateProperty):
    def __get__(self, obj, objtype):
        if not obj.delegate:
            raise pymongo.errors.InvalidOperation(
                "Call open() on %s before accessing attribute '%s'" % (
                    obj.__class__.__name__, self.name))
        return getattr(obj.delegate, self.name)

    def __set__(self, obj, val):
        raise AttributeError


class ReadWriteDelegateProperty(ReadOnlyDelegateProperty):
    def __set__(self, obj, val):
        if not obj.delegate:
            raise pymongo.errors.InvalidOperation(
                "Call open() on %s before accessing attribute '%s'" % (
                    obj.__class__.__name__, self.name))
        setattr(obj.delegate, self.name, val)


class MotorMeta(type):
    def __new__(cls, name, bases, attrs):
        # Create the class.
        new_class = type.__new__(cls, name, bases, attrs)

        # Set DelegateProperties' names
        for name, attr in attrs.items():
            if isinstance(attr, DelegateProperty):
                attr.set_name(name)

        return new_class


class MotorBase(object):
    __metaclass__ = MotorMeta

    def __eq__(self, other):
        if isinstance(other, self.__class__):
            return self.delegate == other.delegate
        return NotImplemented

    get_lasterror_options           = ReadOnlyDelegateProperty()
    set_lasterror_options           = ReadOnlyDelegateProperty()
    unset_lasterror_options         = ReadOnlyDelegateProperty()
    name                            = ReadOnlyDelegateProperty()
    document_class                  = ReadWriteDelegateProperty()
    slave_okay                      = ReadWriteDelegateProperty()
    safe                            = ReadWriteDelegateProperty()
    read_preference                 = ReadWriteDelegateProperty()
    tag_sets                        = ReadWriteDelegateProperty()
    secondary_acceptable_latency_ms = ReadWriteDelegateProperty()
    write_concern                   = ReadWriteDelegateProperty()

    def __repr__(self):
        return '%s(%s)' % (self.__class__.__name__, repr(self.delegate))


class MotorOpenable(object):
    """Base class for Motor objects that can be initialized in two stages:
       basic setup in __init__ and setup requiring I/O in open(), which
       creates the delegate object.
    """
    __metaclass__ = MotorMeta
    __delegate_class__ = None

    def __init__(self, delegate, io_loop, *args, **kwargs):
        if io_loop:
            if not isinstance(io_loop, ioloop.IOLoop):
                raise TypeError(
                    "io_loop must be instance of IOLoop, not %s" % (
                        repr(io_loop)))
            self.io_loop = io_loop
        else:
            self.io_loop = ioloop.IOLoop.instance()

        self.delegate = delegate

        # Store args and kwargs for when open() is called
        self._init_args = args
        self._init_kwargs = kwargs

    def get_io_loop(self):
        return self.io_loop

    def open(self, callback):
        """
        Actually initialize, passing self to a callback when complete.

        :Parameters:
         - `callback`: Optional function taking parameters (self, error)
        """
        self._open(callback)

    def _open(self, callback):
        check_callable(callback)

        if self.delegate:
            # Already open
            if callback:
                self.io_loop.add_callback(
                    functools.partial(callback, self, None))
            return

        def _connect():
            # Run on child greenlet
            error = None
            try:
                args, kwargs = self._delegate_init_args()
                self.delegate = self.__delegate_class__(*args, **kwargs)
            except Exception, e:
                error = e

            if callback:
                # Schedule callback to be executed on main greenlet, with
                # (self, None) if no error, else (None, error)
                self.io_loop.add_callback(
                    functools.partial(
                        callback, None if error else self, error))

        # Actually connect on a child greenlet
        gr = greenlet.greenlet(_connect)
        gr.switch()

    def _delegate_init_args(self):
        """Return args, kwargs to create a delegate object"""
        return self._init_args, self._init_kwargs


class MotorClientBase(MotorOpenable, MotorBase):
    """MotorClient and MotorReplicaSetClient common functionality.
    """
    database_names = AsyncRead()
    server_info    = AsyncRead()
    close_cursor   = AsyncCommand()
    copy_database  = AsyncCommand()
    drop_database  = AsyncCommand().unwrap('MotorDatabase')
    disconnect     = ReadOnlyDelegateProperty()
    tz_aware       = ReadOnlyDelegateProperty()
    close          = ReadOnlyDelegateProperty()
    is_primary     = ReadOnlyDelegateProperty()
    is_mongos      = ReadOnlyDelegateProperty()
    max_bson_size  = ReadOnlyDelegateProperty()
    max_pool_size  = ReadOnlyDelegateProperty()

    def open_sync(self):
        """Synchronous open(), returning self.

        Under the hood, this method creates a new Tornado IOLoop, runs
        :meth:`open` on the loop, and deletes the loop when :meth:`open`
        completes.
        """
        if self.connected:
            return self

        # Run a private IOLoop until connected or error
        private_loop = ioloop.IOLoop()
        standard_loop, self.io_loop = self.io_loop, private_loop
        try:
            outcome = {}
            def callback(connection, error):
                outcome['error'] = error
                self.io_loop.stop()

            self._open(callback)

            # Returns once callback has been executed and loop stopped.
            self.io_loop.start()
        finally:
            # Replace the private IOLoop with the default loop
            self.io_loop = standard_loop
            if self.delegate:
                self.delegate.pool_class = functools.partial(
                    MotorPool, self.io_loop)

                for pool in self._get_pools():
                    pool.io_loop = self.io_loop
                    pool.reset()

            # Clean up file descriptors.
            private_loop.close()

        if outcome['error']:
            raise outcome['error']

        return self

    def sync_client(self):
        return self.__delegate_class__(
            *self._init_args, **self._init_kwargs)

    def __getattr__(self, name):
        if not self.connected:
            msg = ("Can't access attribute '%s' on %s before calling open()"
                  " or open_sync()" % (
                name, self.__class__.__name__))
            raise pymongo.errors.InvalidOperation(msg)

        return MotorDatabase(self, name)

    __getitem__ = __getattr__

    def start_request(self):
        raise NotImplementedError("Motor doesn't implement requests")

    in_request = end_request = start_request

    @property
    def connected(self):
        """True after :meth:`open` or :meth:`open_sync` completes"""
        return self.delegate is not None

    def _delegate_init_args(self):
        """Override MotorOpenable._delegate_init_args to ensure
           auto_start_request is False and _pool_class is MotorPool.
        """
        kwargs = self._init_kwargs.copy()
        kwargs['auto_start_request'] = False
        kwargs['_pool_class'] = functools.partial(MotorPool, self.io_loop)
        return self._init_args, kwargs


class MotorClient(MotorClientBase):
    __delegate_class__ = pymongo.mongo_client.MongoClient

    kill_cursors = AsyncCommand()
    fsync        = AsyncCommand()
    unlock       = AsyncCommand()
    nodes        = ReadOnlyDelegateProperty()
    host         = ReadOnlyDelegateProperty()
    port         = ReadOnlyDelegateProperty()

    def __init__(self, *args, **kwargs):
        """Create a new connection to a single MongoDB instance at *host:port*.

        :meth:`open` or :meth:`open_sync` must be called before using a new
        MotorClient. No property access is allowed before the connection
        is opened.

        MotorClient takes the same constructor arguments as
        :class:`~pymongo.mongo_client.MongoClient`, as well as:

        :Parameters:
          - `io_loop` (optional): Special :class:`tornado.ioloop.IOLoop`
            instance to use instead of default
        """
        super(MotorClient, self).__init__(
            None, kwargs.pop('io_loop', None), *args, **kwargs)

    def is_locked(self, callback):
        """Passes ``True`` to the callback if this server is locked, otherwise
        ``False``. While locked, all write operations are blocked, although
        read operations may still be allowed.
        Use :meth:`fsync` to lock, :meth:`unlock` to unlock::

            @gen.engine
            def lock_unlock():
                c = yield motor.Op(motor.MotorClient().open)
                locked = yield motor.Op(c.is_locked)
                assert locked is False
                yield motor.Op(c.fsync, lock=True)
                assert (yield motor.Op(c.is_locked)) is True
                yield motor.Op(c.unlock)
                assert (yield motor.Op(c.is_locked)) is False

        :Parameters:
         - `callback`:    function taking parameters (result, error)

        .. note:: PyMongo's :attr:`~pymongo.mongo_client.MongoClient.is_locked`
           is a property that synchronously executes the `currentOp` command on
           the server before returning. In Motor, `is_locked` must take a
           callback and executes asynchronously.
        """
        def is_locked(result, error):
            if error:
                callback(None, error)
            else:
                callback(bool(result.get('fsyncLock')), None)

        self.admin.current_op(callback=is_locked)

    def _get_pools(self):
        # TODO: expose the PyMongo pool, or otherwise avoid this
        return [self.delegate._MongoClient__pool]


class MotorReplicaSetClient(MotorClientBase):
    __delegate_class__ = pymongo.mongo_replica_set_client.MongoReplicaSetClient

    primary     = ReadOnlyDelegateProperty()
    secondaries = ReadOnlyDelegateProperty()
    arbiters    = ReadOnlyDelegateProperty()
    hosts       = ReadOnlyDelegateProperty()
    seeds       = ReadOnlyDelegateProperty()
    close       = ReadOnlyDelegateProperty()

    def __init__(self, *args, **kwargs):
        """Create a new connection to a MongoDB replica set.

        :meth:`open` or :meth:`open_sync` must be called before using a new
        MotorReplicaSetClient. No property access is allowed before the
        connection is opened.

        MotorReplicaSetClient takes the same constructor arguments as
        :class:`~pymongo.mongo_replica_set_client.MongoReplicaSetClient`,
        as well as:

        :Parameters:
          - `io_loop` (optional): Special :class:`tornado.ioloop.IOLoop`
            instance to use instead of default
        """
        # We only override __init__ to replace its docstring
        super(MotorReplicaSetClient, self).__init__(
            None, kwargs.pop('io_loop', None), *args, **kwargs)

    def open_sync(self):
        """Synchronous open(), returning self.

        Under the hood, this method creates a new Tornado IOLoop, runs
        :meth:`open` on the loop, and deletes the loop when :meth:`open`
        completes.
        """
        super(MotorReplicaSetClient, self).open_sync()

        # We need to wait for open_sync() to complete and restore the
        # original IOLoop before starting the monitor.
        self.delegate._MongoReplicaSetClient__monitor.start_motor(self.io_loop)
        return self

    def open(self, callback):
        check_callable(callback)

        def opened(result, error):
            if error:
                callback(None, error)
            else:
                try:
                    monitor = self.delegate._MongoReplicaSetClient__monitor
                    monitor.start_motor(self.io_loop)
                except Exception, e:
                    callback(None, e)
                else:
                    # No errors
                    callback(self, None)

        super(MotorReplicaSetClient, self)._open(callback=opened)

    def _delegate_init_args(self):
        # This _monitor_class will be passed to PyMongo's
        # MongoReplicaSetClient when we create it.
        args, kwargs = super(
            MotorReplicaSetClient, self)._delegate_init_args()
        kwargs['_monitor_class'] = MotorReplicaSetMonitor
        return args, kwargs

    def _get_pools(self):
        # TODO: expose the PyMongo RSC members, or otherwise avoid this
        return [
            member.pool for member in
            self.delegate._MongoReplicaSetClient__members.values()]


# PyMongo uses a background thread to regularly inspect the replica set and
# monitor it for changes. In Motor, use a periodic callback on the IOLoop to
# monitor the set.
class MotorReplicaSetMonitor(pymongo.mongo_replica_set_client.Monitor):
    def __init__(self, rsc):
        assert isinstance(
            rsc, pymongo.mongo_replica_set_client.MongoReplicaSetClient), (
            "First argument to MotorReplicaSetMonitor must be"
            " MongoReplicaSetClient, not %s" % repr(rsc))

        # Fake the event_class: we won't use it
        pymongo.mongo_replica_set_client.Monitor.__init__(
            self, rsc, event_class=object)

        self.timeout_obj = None

    def shutdown(self, dummy=None):
        if self.timeout_obj:
            self.io_loop.remove_timeout(self.timeout_obj)

    def refresh(self):
        assert greenlet.getcurrent().parent, "Should be on child greenlet"
        try:
            self.rsc.refresh()
        except pymongo.errors.AutoReconnect:
            pass
        # RSC has been collected or there
        # was an unexpected error.
        except:
            return

        self.timeout_obj = self.io_loop.add_timeout(
            time.time() + self._refresh_interval, self.async_refresh)

    def start(self):
        """No-op: PyMongo thinks this starts the monitor, but Motor starts
           the monitor separately to ensure it uses the right IOLoop"""
        pass

    def start_motor(self, io_loop):
        self.io_loop = io_loop
        self.async_refresh = asynchronize(
            self.io_loop,
            self.refresh,
            has_safe_arg=False,
            callback_required=False)
        self.timeout_obj = self.io_loop.add_timeout(
            time.time() + self._refresh_interval, self.async_refresh)

    def schedule_refresh(self):
        if self.io_loop and self.async_refresh:
            if self.timeout_obj:
                self.io_loop.remove_timeout(self.timeout_obj)

            self.io_loop.add_callback(self.async_refresh)

    def join(self, timeout=None):
        # PyMongo calls join() after shutdown() -- this is not a thread, so
        # shutdown works immediately and join is unnecessary
        pass


class MotorDatabase(MotorBase):
    __delegate_class__ = Database

    set_profiling_level = AsyncCommand()
    reset_error_history = AsyncCommand()
    add_user            = AsyncCommand()
    remove_user         = AsyncCommand()
    logout              = AsyncCommand()
    command             = AsyncCommand()
    authenticate        = AsyncCommand()
    eval                = AsyncCommand()
    create_collection   = AsyncCommand().wrap(Collection)
    drop_collection     = AsyncCommand().unwrap('MotorCollection')
    validate_collection = AsyncRead().unwrap('MotorCollection')
    collection_names    = AsyncRead()
    current_op          = AsyncRead()
    profiling_level     = AsyncRead()
    profiling_info      = AsyncRead()
    error               = AsyncRead()
    last_status         = AsyncRead()
    previous_error      = AsyncRead()
    dereference         = AsyncRead()

    incoming_manipulators         = ReadOnlyDelegateProperty()
    incoming_copying_manipulators = ReadOnlyDelegateProperty()
    outgoing_manipulators         = ReadOnlyDelegateProperty()
    outgoing_copying_manipulators = ReadOnlyDelegateProperty()

    def __init__(self, connection, name):
        if not isinstance(connection, MotorClientBase):
            raise TypeError("First argument to MotorDatabase must be "
                            "MotorClientBase, not %s" % repr(connection))

        self.connection = connection
        self.delegate = Database(connection.delegate, name)

    def __getattr__(self, name):
        return MotorCollection(self, name)

    __getitem__ = __getattr__

    def wrap(self, collection):
        # Replace pymongo.collection.Collection with MotorCollection
        return self[collection.name]

    def add_son_manipulator(self, manipulator):
        """Add a new son manipulator to this database.

        Newly added manipulators will be applied before existing ones.

        :Parameters:
          - `manipulator`: the manipulator to add
        """
        # We override add_son_manipulator to unwrap the AutoReference's
        # database attribute.
        if isinstance(manipulator, pymongo.son_manipulator.AutoReference):
            db = manipulator.database
            if isinstance(db, MotorDatabase):
                manipulator.database = db.delegate

        self.delegate.add_son_manipulator(manipulator)

    def get_io_loop(self):
        return self.connection.get_io_loop()


class MotorCollection(MotorBase):
    __delegate_class__ = Collection

    create_index      = AsyncCommand()
    drop_indexes      = AsyncCommand()
    drop_index        = AsyncCommand()
    drop              = AsyncCommand()
    ensure_index      = AsyncCommand()
    reindex           = AsyncCommand()
    rename            = AsyncCommand()
    find_and_modify   = AsyncCommand()
    map_reduce        = AsyncCommand().wrap(Collection)
    update            = AsyncWrite()
    insert            = AsyncWrite()
    remove            = AsyncWrite()
    save              = AsyncWrite()
    index_information = AsyncRead()
    count             = AsyncRead()
    options           = AsyncRead()
    group             = AsyncRead()
    distinct          = AsyncRead()
    inline_map_reduce = AsyncRead()
    find_one          = AsyncRead()
    aggregate         = AsyncRead()
    uuid_subtype      = ReadWriteDelegateProperty()
    full_name         = ReadOnlyDelegateProperty()

    def __init__(self, database, name=None, *args, **kwargs):
        if isinstance(database, Collection):
            # Short cut
            self.delegate = Collection
        elif not isinstance(database, MotorDatabase):
            raise TypeError("First argument to MotorCollection must be "
                            "MotorDatabase, not %s" % repr(database))
        else:
            self.database = database
            self.delegate = Collection(self.database.delegate, name)

    def __getattr__(self, name):
        # dotted collection name, like foo.bar
        return MotorCollection(
            self.database,
            self.name + '.' + name
        )

    def find(self, *args, **kwargs):
        """Create a :class:`MotorCursor`. Same parameters as for
        :meth:`~pymongo.collection.Collection.find`.

        Note that :meth:`find` does not take a `callback` parameter -- pass
        a callback to the :class:`MotorCursor`'s methods such as
        :meth:`MotorCursor.find`.

        # TODO: examples of to_list and next
        """
        if 'callback' in kwargs:
            raise pymongo.errors.InvalidOperation(
                "Pass a callback to fetch_next, each, to_list, count, or tail,"
                "not to find"
            )

        cursor = self.delegate.find(*args, **kwargs)
        return MotorCursor(cursor, self)

    def wrap(self, collection):
        # Replace pymongo.collection.Collection with MotorCollection
        return self.database[collection.name]

    def get_io_loop(self):
        return self.database.get_io_loop()


class MotorCursorChainingMethod(DelegateProperty):
    def __get__(self, obj, objtype):
        method = getattr(obj.delegate, self.name)

        @functools.wraps(method)
        def return_clone(*args, **kwargs):
            method(*args, **kwargs)
            return obj

        return return_clone


class MotorCursor(MotorBase):
    __delegate_class__ = Cursor
    count         = AsyncRead()
    distinct      = AsyncRead()
    explain       = AsyncRead()
    _refresh      = AsyncRead()
    close         = AsyncCommand()
    cursor_id     = ReadOnlyDelegateProperty()
    alive         = ReadOnlyDelegateProperty()
    __copy__      = ReadOnlyDelegateProperty().wrap(Cursor)
    __deepcopy__  = ReadOnlyDelegateProperty().wrap(Cursor)
    batch_size    = MotorCursorChainingMethod()
    add_option    = MotorCursorChainingMethod()
    remove_option = MotorCursorChainingMethod()
    limit         = MotorCursorChainingMethod()
    skip          = MotorCursorChainingMethod()
    max_scan      = MotorCursorChainingMethod()
    sort          = MotorCursorChainingMethod()
    hint          = MotorCursorChainingMethod()
    where         = MotorCursorChainingMethod()

    def __init__(self, cursor, collection):
        """You will not usually construct a MotorCursor yourself, but acquire
        one from :meth:`MotorCollection.find`.

        :Parameters:
         - `cursor`:      PyMongo :class:`~pymongo.cursor.Cursor`
         - `collection`:  :class:`MotorCollection`

        .. note::
          There is no need to manually close cursors; they are closed
          by the server after being fully iterated
          with :meth:`to_list`, :meth:`each`, or :meth:`fetch_next`, or
          automatically closed by the client when the :class:`MotorCursor` is
          cleaned up by the garbage collector.
        """
        if not isinstance(cursor, Cursor):
            raise TypeError(
                "cursor must be instance of pymongo.cursor.Cursor, not %s" % (
                repr(cursor)))

        if not isinstance(collection, MotorCollection):
            raise TypeError(
                "collection must be instance of MotorCollection, not %s" % (
                repr(collection)))

        self.delegate = cursor
        self.collection = collection
        self.started = False

    def _get_more(self, callback):
        """
        Get a batch of data asynchronously, either performing an initial query
        or getting more data from an existing cursor.
        :Parameters:
         - `callback`:    function taking parameters (batch_size, error)
        """
        if not self.alive:
            raise pymongo.errors.InvalidOperation(
                "Can't call get_more() on a MotorCursor that has been"
                " exhausted or killed."
            )

        self.started = True
        self._refresh(callback=callback)

    @property
    def fetch_next(self):
        """Used with `gen.engine`_ to asynchronously retrieve the next document
        in the result set, fetching a batch of documents from the server if
        necessary. Yields ``False`` if there are no more documents, otherwise
        :meth:`next_object` is guaranteed to return a document.

        .. _`gen.engine`: http://www.tornadoweb.org/documentation/gen.html

        .. testsetup:: fetch_next

          import sys

          from pymongo.mongo_client import MongoClient
          MongoClient().test.test_collection.remove(safe=True)

          import motor
          from motor import MotorClient
          from tornado.ioloop import IOLoop
          from tornado import gen
          collection = MotorClient().open_sync().test.test_collection

        .. doctest:: fetch_next

          >>> @gen.engine
          ... def f():
          ...     yield motor.Op(collection.insert,
          ...         [{'_id': i} for i in range(5)])
          ...
          ...     cursor = collection.find().sort([('_id', 1)])
          ...     while (yield cursor.fetch_next):
          ...         doc = cursor.next_object()
          ...         sys.stdout.write(str(doc['_id']) + ', ')
          ...     print 'done'
          ...     IOLoop.instance().stop()
          ...
          >>> f()
          >>> IOLoop.instance().start()
          0, 1, 2, 3, 4, done

        .. note:: While it appears that fetch_next retrieves each document from
          the server individually, the cursor actually fetches documents
          efficiently in `large batches`_.

        .. _`large batches`: http://www.mongodb.org/display/DOCS/Queries+and+Cursors#QueriesandCursors-Executionofqueriesinbatches
        """
        # TODO: prevent concurrent uses of this cursor, as IOStream does, and
        #   document that and how to avoid it.
        # TODO: optimize, always return same FetchNext instance? Or even make
        #   this cursor a YieldPoint?
        return FetchNext(self)

    def next_object(self):
        """Get a document from the most recently fetched batch, or ``None``.
        See :attr:`fetch_next`.
        """
        # __empty is a special case: limit of 0
        if self.delegate._Cursor__empty or not self.buffer_size:
            return None
        return self.delegate.next()

    def each(self, callback):
        """Iterates over all the documents for this cursor.

        `each` returns immediately, and `callback` is executed asynchronously
        for each document. `callback` is passed ``(None, None)`` when iteration
        is complete.

        Cancel iteration early by returning ``False`` from the callback. (Only
        ``False`` cancels iteration: returning ``None`` or 0 does not.)

        .. testsetup:: each

          import sys

          from pymongo.mongo_client import MongoClient
          MongoClient().test.test_collection.remove()
          from motor import MotorClient
          from tornado.ioloop import IOLoop
          collection = MotorClient().open_sync().test.test_collection

        .. doctest:: each

          >>> cursor = None
          >>> def inserted(result, error):
          ...     global cursor
          ...     if error:
          ...         raise error
          ...     cursor = collection.find().sort([('_id', 1)])
          ...     cursor.each(callback=each)
          ...
          >>> def each(result, error):
          ...     if error:
          ...         raise error
          ...     elif result:
          ...         sys.stdout.write(str(result['_id']) + ', ')
          ...     else:
          ...         # Iteration complete
          ...         IOLoop.instance().stop()
          ...         print 'done'
          ...
          >>> collection.insert(
          ...     [{'_id': i} for i in range(5)], callback=inserted)
          >>> IOLoop.instance().start()
          0, 1, 2, 3, 4, done

        :Parameters:
         - `callback`: function taking (document, error)
        """
        check_callable(callback, required=True)
        self._each_got_more(callback, None, None)

    def _each_got_more(self, callback, batch_size, error):
        add_callback = self.get_io_loop().add_callback
        if error:
            callback(None, error)
            return

        while self.buffer_size > 0:
            try:
                doc = self.delegate.next() # decrements self.buffer_size
            except StopIteration:
                # Special case: limit of 0
                add_callback(functools.partial(callback, None, None))
                self.close()
                return

            # Quit if callback returns exactly False (not None). Note we
            # don't close the cursor: user may want to resume iteration.
            if callback(doc, None) is False:
                return

        if self.alive and (self.cursor_id or not self.started):
            self._get_more(functools.partial(self._each_got_more, callback))
        else:
            # Complete
            add_callback(functools.partial(callback, None, None))

    def to_list(self, length=None, callback=None):
        """Get a list of documents.

        The caller is responsible for making sure that there is enough memory
        to store the results -- it is strongly recommended either that you
        specify a ``length`` and loop until all documents are consumed:

        .. testsetup:: to_list

          from pymongo.mongo_client import MongoClient
          MongoClient().test.test_collection.remove(safe=True)
          from motor import MotorClient
          from tornado.ioloop import IOLoop
          from tornado import gen
          collection = MotorClient().open_sync().test.test_collection
          import motor

        .. doctest:: to_list

          >>> @gen.engine
          ... def f():
          ...     yield motor.Op(
          ...         collection.insert, [{'_id': i} for i in range(4)])
          ...     cursor = collection.find().sort([('_id', 1)])
          ...     docs = yield motor.Op(cursor.to_list, 2) # length is 2
          ...     while docs:
          ...         print docs
          ...         docs = (yield motor.Op(cursor.to_list, 2))
          ...
          ...     print 'done'
          ...     IOLoop.instance().stop()
          ...
          >>> f()
          >>> IOLoop.instance().start()
          [{u'_id': 0}, {u'_id': 1}]
          [{u'_id': 2}, {u'_id': 3}]
          done

        Or that you set a limit on the cursor:

        .. doctest:: to_list

          >>> @gen.engine
          ... def f():
          ...     yield motor.Op(
          ...         collection.insert, [{'_id': i} for i in range(4, 400)])
          ...     cursor = collection.find().sort([('_id', 1)]).limit(4)
          ...     # No max length passed to 'to_list', but a limit set on cursor
          ...     docs = yield motor.Op(cursor.to_list)
          ...     print docs
          ...     print 'done'
          ...     IOLoop.instance().stop()
          ...
          >>> f()
          >>> IOLoop.instance().start()
          [{u'_id': 0}, {u'_id': 1}, {u'_id': 2}, {u'_id': 3}]
          done

        ``to_list`` returns immediately, and ``callback`` is executed
        asynchronously with the list of documents.

        :Parameters:
         - `length`: optional, maximum number of documents to return for this
           call
         - `callback`: function taking (documents, error)
        """
        length = pymongo.common.validate_positive_integer_or_none(
            'length', length)

        if self.delegate._Cursor__tailable:
            raise pymongo.errors.InvalidOperation(
                "Can't call to_list on tailable cursor")

        check_callable(callback, required=True)
        the_list = []

        # Special case
        if self.delegate._Cursor__empty:
            callback([], None)
            return

        self._to_list_got_more(callback, the_list, length, None, None)

    def _to_list_got_more(self, callback, the_list, length, batch_size, error):
        if error:
            callback(None, error)
            return

        if length is None:
            # No maximum length, get all results
            the_list.extend(self.delegate._Cursor__data)
            self.delegate._Cursor__data.clear()
        else:
            while self.buffer_size > 0 and len(the_list) < length:
                the_list.append(self.delegate._Cursor__data.popleft())

        if (
            not self.delegate._Cursor__killed
            and (self.cursor_id or not self.started)
            and (length is None or length > len(the_list))
        ):
            get_more_cb = functools.partial(
                self._to_list_got_more, callback, the_list, length)
            self._get_more(callback=get_more_cb)
        else:
            callback(the_list, None)

    def clone(self):
        return MotorCursor(self.delegate.clone(), self.collection)

    def rewind(self):
        self.delegate.rewind()
        self.started = False
        return self

    def tail(self, callback):
        # TODO: doc, prominently. await_data is always true.
        # TODO: doc that tailing an empty collection is expensive,
        #   consider a failsafe, e.g. timing the interval between getmore
        #   and return, and if it's short and no doc, pause before next
        #   getmore
        # TODO: doc that cursor is closed if iteration cancelled
        check_callable(callback, True)

        cursor = self.clone()

        cursor.delegate._Cursor__tailable = True
        cursor.delegate._Cursor__await_data = True

        # Start tailing
        cursor.each(functools.partial(self._tail_got_more, cursor, callback))

    def _tail_got_more(self, cursor, callback, result, error):
        if error:
            cursor.close()
            callback(None, error)
        elif result is not None:
            if callback(result, None) is False:
                # Callee cancelled tailing
                cursor.close()
                return False

        if not cursor.alive:
            # cursor died, start over soon
            self.get_io_loop().add_timeout(
                time.time() + 0.5,
                functools.partial(cursor.tail, callback))

    def get_io_loop(self):
        return self.collection.get_io_loop()

    @property
    def buffer_size(self):
        # TODO: expose so we don't have to use double-underscore hack
        return len(self.delegate._Cursor__data)

    def __getitem__(self, index):
        """Get a slice of documents from this cursor.

        Raises :class:`~pymongo.errors.InvalidOperation` if this
        cursor has already been used.

        To get a single document use an integral index, e.g.:

        .. testsetup:: getitem

          import sys
          from pymongo.mongo_client import MongoClient
          MongoClient().test.test_collection.remove(safe=True)
          from motor import MotorClient
          from tornado.ioloop import IOLoop
          from tornado import gen
          collection = MotorClient().open_sync().test.test_collection
          import motor

        .. doctest:: getitem

          >>> @gen.engine
          ... def f():
          ...     yield motor.Op(collection.insert,
          ...         [{'i': i} for i in range(10)])
          ...
          ...     cursor = collection.find().sort([('i', 1)])[5]
          ...     yield cursor.fetch_next
          ...     doc = cursor.next_object()
          ...     print doc['i']
          ...     IOLoop.instance().stop()
          ...
          >>> f()
          >>> IOLoop.instance().start()
          5

        Any limit previously applied to this cursor will be ignored.

        The cursor returns ``None`` if the index is greater than or equal to
        the length of the result set.

        .. doctest:: getitem

          >>> @gen.engine
          ... def f():
          ...     cursor = collection.find().sort([('i', 1)])[1000]
          ...     yield cursor.fetch_next
          ...     print cursor.next_object()
          ...     IOLoop.instance().stop()
          ...
          >>> f()
          >>> IOLoop.instance().start()
          None

        To get a slice of documents use a slice index like
        ``cursor[start:end]``.

        .. doctest:: getitem

          >>> @gen.engine
          ... def f():
          ...     cursor = collection.find().sort([('i', 1)])[2:6]
          ...     while (yield cursor.fetch_next):
          ...         doc = cursor.next_object()
          ...         sys.stdout.write(str(doc['i']) + ', ')
          ...     print 'done'
          ...     IOLoop.instance().stop()
          ...
          >>> f()
          >>> IOLoop.instance().start()
          2, 3, 4, 5, done

        This will return a copy of this cursor with a limit of ``5`` and
        skip of ``20`` applied.  Using a slice index will override any prior
        limits or skips applied to this cursor (including those
        applied through previous calls to this method).

        Raises :class:`~pymongo.errors.IndexError` when the slice has a step,
        a negative start value, or a stop value less than or equal to
        the start value.

        :Parameters:
          - `index`: An integer or slice index to be applied to this cursor
        """
        if self.started:
            raise pymongo.errors.InvalidOperation("MotorCursor already started")

        if isinstance(index, slice):
            # Slicing a cursor does no I/O - it just sets skip and limit - so
            # we can slice it immediately.
            self.delegate[index]
            return self
        else:
            if not isinstance(index, (int, long)):
                raise TypeError("index %r cannot be applied to MotorCursor "
                                "instances" % index)

            # Get one document, force hard limit of 1 so server closes cursor
            # immediately
            return self[self.delegate._Cursor__skip+index:].limit(-1)

    def wrap(self, cursor):
        # Replace pymongo.cursor.Cursor with MotorCursor
        return MotorCursor(cursor, self.collection)

    def __del__(self):
        # This MotorCursor is deleted on whatever greenlet does the last decref,
        # or (if it's referenced from a cycle) whichever is current when the GC
        # kicks in. We may need to send the server a killCursors message, but in
        # Motor only direct children of the main greenlet can do I/O. First, do
        # a quick check whether the cursor is still alive on the server:
        if self.cursor_id and self.alive:
            if greenlet.getcurrent().parent:
                # We're on a child greenlet, send the message.
                self.delegate.close()
            else:
                # We're on the main greenlet, start the operation on a child.
                self.close()


class MotorGridOut(MotorOpenable):
    """Class to read data out of GridFS.

       Application developers should generally not need to
       instantiate this class directly - instead see the methods
       provided by :class:`~motor.MotorGridFS`.
    """
    __delegate_class__ = gridfs.GridOut

    __getattr__     = ReadOnlyDelegateProperty()
    _id             = ReadOnlyDelegateProperty()
    filename        = ReadOnlyDelegateProperty()
    name            = ReadOnlyDelegateProperty()
    content_type    = ReadOnlyDelegateProperty()
    length          = ReadOnlyDelegateProperty()
    chunk_size      = ReadOnlyDelegateProperty()
    upload_date     = ReadOnlyDelegateProperty()
    aliases         = ReadOnlyDelegateProperty()
    metadata        = ReadOnlyDelegateProperty()
    md5             = ReadOnlyDelegateProperty()
    tell            = ReadOnlyDelegateProperty()
    seek            = ReadOnlyDelegateProperty()
    read            = AsyncRead()
    readline        = AsyncRead()

    def __init__(
        self, root_collection, file_id=None, file_document=None,
        io_loop=None
    ):
        if isinstance(root_collection, grid_file.GridOut):
            # Short cut
            MotorOpenable.__init__(
                self, root_collection, io_loop)
        else:
            if not isinstance(root_collection, MotorCollection):
                raise TypeError(
                    "First argument to MotorGridOut must be "
                    "MotorCollection, not %s" % repr(root_collection))

            assert io_loop is None, \
                "Can't override IOLoop for MotorGridOut"

            MotorOpenable.__init__(
                self, None, root_collection.get_io_loop(),
                root_collection.delegate, file_id, file_document)

    @gen.engine
    def stream_to_handler(self, request_handler, callback=None):
        """Write the contents of this file to a
        :class:`tornado.web.RequestHandler`. This method will call `flush` on
        the RequestHandler, so ensure all headers have already been set.
        For a more complete example see the implementation of
        :class:`~motor.web.GridFSHandler`.

        .. code-block:: python

            class FileHandler(tornado.web.RequestHandler):
                @tornado.web.asynchronous
                @gen.engine
                def get(self, filename):
                    db = self.settings['db']
                    fs = yield motor.Op(motor.MotorGridFS(db).open)
                    try:
                        gridout = yield motor.Op(fs.get_last_version, filename)
                    except gridfs.NoFile:
                        raise tornado.web.HTTPError(404)

                    self.set_header("Content-Type", gridout.content_type)
                    self.set_header("Content-Length", gridout.length)
                    yield motor.Op(gridout.stream_to_handler, self)
                    self.finish()

        .. seealso:: Tornado `RequestHandler <http://www.tornadoweb.org/documentation/web.html#request-handlers>`_
        """
        check_callable(callback, False)

        try:
            written = 0
            while written < self.length:
                # Reading chunk_size at a time minimizes buffering
                chunk = yield Op(self.read, self.chunk_size)

                # write() simply appends the output to a list; flush() sends it
                # over the network and minimizes buffering in the handler.
                request_handler.write(chunk)
                request_handler.flush()
                written += len(chunk)
            if callback:
                callback(None, None)
        except Exception, e:
            if callback:
                callback(None, e)


class MotorGridIn(MotorOpenable):
    __delegate_class__ = gridfs.GridIn

    __getattr__     = ReadOnlyDelegateProperty()
    closed          = ReadOnlyDelegateProperty()
    close           = AsyncCommand()
    write           = AsyncCommand().unwrap(MotorGridOut)
    writelines      = AsyncCommand().unwrap(MotorGridOut)
    _id             = ReadOnlyDelegateProperty()
    md5             = ReadOnlyDelegateProperty()
    filename        = ReadOnlyDelegateProperty()
    name            = ReadOnlyDelegateProperty()
    content_type    = ReadOnlyDelegateProperty()
    length          = ReadOnlyDelegateProperty()
    chunk_size      = ReadOnlyDelegateProperty()
    upload_date     = ReadOnlyDelegateProperty()
    set             = AsyncCommand(name='__setattr__')

    def __init__(self, root_collection, **kwargs):
        """
        Class to write data to GridFS. If instantiating directly, you must call
        :meth:`open` before using the `MotorGridIn` object. However, application
        developers should generally not need to instantiate this class - instead
        see the methods provided by :class:`~motor.MotorGridFS`.

        Any of the file level options specified in the `GridFS Spec
        <http://dochub.mongodb.org/core/gridfsspec>`_ may be passed as
        keyword arguments. Any additional keyword arguments will be
        set as additional fields on the file document. Valid keyword
        arguments include:

          - ``"_id"``: unique ID for this file (default:
            :class:`~bson.objectid.ObjectId`) - this ``"_id"`` must
            not have already been used for another file

          - ``"filename"``: human name for the file

          - ``"contentType"`` or ``"content_type"``: valid mime-type
            for the file

          - ``"chunkSize"`` or ``"chunk_size"``: size of each of the
            chunks, in bytes (default: 256 kb)

          - ``"encoding"``: encoding used for this file. In Python 2,
            any :class:`unicode` that is written to the file will be
            converted to a :class:`str`. In Python 3, any :class:`str`
            that is written to the file will be converted to
            :class:`bytes`.

        :Parameters:
          - `root_collection`: A :class:`MotorCollection`, the root collection
             to write to
          - `**kwargs` (optional): file level options (see above)
        """
        if isinstance(root_collection, grid_file.GridIn):
            # Short cut
            MotorOpenable.__init__(
                self, root_collection, kwargs.pop('io_loop', None))
        else:
            if not isinstance(root_collection, MotorCollection):
                raise TypeError(
                    "First argument to MotorGridIn must be "
                    "MotorCollection, not %s" % repr(root_collection))

            assert 'io_loop' not in kwargs, \
                "Can't override IOLoop for MotorGridIn"
            MotorOpenable.__init__(
                self, None, root_collection.get_io_loop(),
                root_collection.delegate, **kwargs)


class MotorGridFS(MotorOpenable):
    __delegate_class__ = gridfs.GridFS

    new_file            = AsyncRead().wrap(grid_file.GridIn)
    get                 = AsyncRead().wrap(grid_file.GridOut)
    get_version         = AsyncRead().wrap(grid_file.GridOut)
    get_last_version    = AsyncRead().wrap(grid_file.GridOut)
    list                = AsyncRead()
    exists              = AsyncRead()
    put                 = AsyncCommand()
    delete              = AsyncCommand()

    def __init__(self, database, collection="fs"):
        """
        An instance of GridFS on top of a single Database. You must call
        :meth:`open` before using the `MotorGridFS` object.

        :Parameters:
          - `database`: a :class:`MotorDatabase`
          - `collection` (optional): A string, name of root collection to use,
            such as "fs" or "my_files"

        .. mongodoc:: gridfs
        """
        if not isinstance(database, MotorDatabase):
            raise TypeError("First argument to MotorGridFS must be "
                            "MotorDatabase, not %s" % repr(database))

        MotorOpenable.__init__(
            self, None, database.get_io_loop(), database.delegate, collection)

    def wrap(self, obj):
        if obj.__class__ is grid_file.GridIn:
            return MotorGridIn(obj, io_loop=self.get_io_loop())
        elif obj.__class__ is grid_file.GridOut:
            return MotorGridOut(obj, io_loop=self.get_io_loop())


if requirements_satisfied:
    class Op(gen.Task):
        def __init__(self, func, *args, **kwargs):
            check_callable(func, True)
            super(Op, self).__init__(func, *args, **kwargs)

        def get_result(self):
            (result, error), _ = super(Op, self).get_result()
            if error:
                raise error
            return result


    class WaitOp(gen.Wait):
        def get_result(self):
            (result, error), _ = super(WaitOp, self).get_result()
            if error:
                raise error

            return result


    class WaitAllOps(gen.WaitAll):
        def get_result(self):
            super_results = super(WaitAllOps, self).get_result()

            results = []
            for (result, error), _ in super_results:
                if error:
                    raise error
                else:
                    results.append(result)

            return results


    class FetchNext(gen.YieldPoint):
        def __init__(self, cursor):
            self.cursor = cursor
            self.error = None
            self.ready = False
            self.runner = None

        def start(self, runner):
            # If cursor's current batch is empty, start fetching next batch...
            cursor = self.cursor
            if not cursor.buffer_size and cursor.alive:
                self.ready = False
                self.runner = runner
                cursor._get_more(self.set_result)
            else:
                self.ready = True

        def set_result(self, result, error):
            # 'result' is _get_more's return value, the number of docs fetched
            self.error = error
            self.ready = True
            runner, self.runner = self.runner, None # Break cycle
            runner.run()

        def is_ready(self):
            # True unless we're in the midst of a fetch
            return self.ready

        def get_result(self):
            if self.error:
                raise self.error
            if self.cursor.delegate._Cursor__empty:
                # Special case, limit of 0
                return False
            return bool(self.cursor.buffer_size)
