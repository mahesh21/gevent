# @author Donovan Preston
#
# Copyright (c) 2007, Linden Research, Inc.
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
from gevent import monkey
monkey.patch_all(thread=False)

import cgi
import os
import urllib2
import sys
try:
    from wsgiref.validate import validator
except ImportError:
    def validator(app):
        return app

import greentest
import gevent
from gevent import socket


CONTENT_LENGTH = 'Content-Length'
CONN_ABORTED_ERRORS = []
server_implements_chunked = True
DEBUG = '-v' in sys.argv

try:
    from errno import WSAECONNABORTED
    CONN_ABORTED_ERRORS.append(WSAECONNABORTED)
except ImportError:
    pass


class ConnectionClosed(Exception):
    pass


def read_headers(fd):
    response_line = fd.readline()
    if not response_line:
        raise ConnectionClosed
    headers = {}
    while True:
        line = fd.readline().strip()
        if not line:
            break
        try:
            key, value = line.split(': ', 1)
        except:
            print 'Failed to split: %r' % (line, )
            raise
        assert key.lower() not in [x.lower() for x in headers.keys()], 'Header %r:%r sent more than once: %r' % (key, value, headers)
        headers[key] = value
    return response_line, headers


def iread_chunks(fd):
    while True:
        line = fd.readline()
        chunk_size = line.strip()
        try:
            chunk_size = int(chunk_size, 16)
        except:
            print 'Failed to parse chunk size: %r' % line
            raise
        if chunk_size == 0:
            crlf = fd.read(2)
            assert crlf == '\r\n', repr(crlf)
            break
        data = fd.read(chunk_size)
        yield data
        crlf = fd.read(2)
        assert crlf == '\r\n', repr(crlf)


class Response(object):

    def __init__(self, status_line, headers, body=None, chunks=None):
        self.status_line = status_line
        self.headers = headers
        self.body = body
        self.chunks = chunks
        try:
            version, code, self.reason = status_line[:-2].split(' ', 2)
        except Exception:
            print 'Error: %r' % status_line
            raise
        self.code = int(code)
        HTTP, self.version = version.split('/')
        assert HTTP == 'HTTP', repr(HTTP)
        assert self.version in ('1.0', '1.1'), repr(self.version)

    def __iter__(self):
        yield self.status_line
        yield self.headers
        yield self.body

    def __str__(self):
        args = (self.__class__.__name__, self.status_line, self.headers, self.body, self.chunks)
        return '<%s status_line=%r headers=%r body=%r chunks=%r>' % args

    def assertCode(self, code):
        assert self.code == code, 'Unexpected code: %r (expected %r)\n%s' % (self.code, code, self)

    def assertReason(self, reason):
        assert self.reason == reason, 'Unexpected reason: %r (expected %r)\n%s' % (self.reason, reason, self)

    def assertVersion(self, version):
        assert self.version == version, 'Unexpected version: %r (expected %r)\n%s' % (self.version, version, self)

    def assertHeader(self, header, value):
        real_value = self.headers.get(header)
        assert real_value == value, \
               'Unexpected header %r: %r (expected %r)\n%s' % (header, real_value, value, self)

    def assertBody(self, body):
        assert self.body == body, \
               'Unexpected body: %r (expected %r)\n%s' % (self.body, body, self)

    @classmethod
    def read(cls, fd, code=200, reason='OK', version='1.1', body=None):
        _status_line, headers = read_headers(fd)
        self = cls(_status_line, headers)
        if code is not None:
            self.assertCode(code)
        if reason is not None:
            self.assertReason(reason)
        if version is not None:
            self.assertVersion(version)
        try:
            if 'chunked' in headers.get('Transfer-Encoding', ''):
                if CONTENT_LENGTH in headers:
                    print "WARNING: server used chunked transfer-encoding despite having Content-Length header (libevent 1.x's bug)"
                self.chunks = list(iread_chunks(fd))
                self.body = ''.join(self.chunks)
            elif CONTENT_LENGTH in headers:
                num = int(headers[CONTENT_LENGTH])
                self.body = fd.read(num)
            else:
                self.body = fd.read()
        except:
            print 'Response.read failed to read the body:\n%s' % self
            raise
        if body is not None:
            self.assertBody(body)
        return self

read_http = Response.read


class DebugFileObject(object):

    def __init__(self, obj):
        self.obj = obj

    def read(self, *args):
        result = self.obj.read(*args)
        if DEBUG:
            print repr(result)
        return result

    def readline(self, *args):
        result = self.obj.readline(*args)
        if DEBUG:
            print repr(result)
        return result

    def __getattr__(self, item):
        assert item != 'obj'
        return getattr(self.obj, item)


def makefile(self, mode='r', bufsize=-1):
    return DebugFileObject(socket._fileobject(self.dup(), mode, bufsize))

socket.socket.makefile = makefile

class TestCase(greentest.TestCase):

    def get_wsgi_module(self):
        from gevent import pywsgi
        return pywsgi

    validator = staticmethod(validator)

    def setUp(self):
        greentest.TestCase.setUp(self)
        application = self.application
        if self.validator is not None:
            application = self.validator(application)
        self.server = self.get_wsgi_module().WSGIServer(('127.0.0.1', 0), application)
        self.server.start()
        self.port = self.server.server_port

    def tearDown(self):
        greentest.TestCase.tearDown(self)
        timeout = gevent.Timeout.start_new(0.5)
        try:
            self.server.stop()
        finally:
            timeout.cancel()
        # XXX currently listening socket is kept open in gevent.wsgi

    def connect(self):
        return socket.create_connection(('127.0.0.1', self.port))


class CommonTests(TestCase):

    def test_basic(self):
        fd = self.connect().makefile(bufsize=1)
        fd.write('GET / HTTP/1.1\r\nHost: localhost\r\n\r\n')
        read_http(fd, body='hello world')
        fd.write('GET /notexist HTTP/1.1\r\nHost: localhost\r\n\r\n')
        read_http(fd, code=404, reason='Not Found', body='not found')
        fd.write('GET / HTTP/1.1\r\nHost: localhost\r\n\r\n')
        read_http(fd, body='hello world')
        fd.close()

    def XXXtest_pipeline(self):
        fd = self.connect().makefile(bufsize=1)
        fd.write('GET / HTTP/1.1\r\nHost: localhost\r\n\r\n' + 'GET /notexist HTTP/1.1\r\nHost: localhost\r\n\r\n')
        read_http(fd, body='hello world')
        exception = AssertionError('HTTP pipelining not supported; the second request is thrown away')
        timeout = gevent.Timeout.start_new(0.5, exception=exception)
        try:
            read_http(fd, code=404, reason='Not Found', body='not found')
            fd.close()
        finally:
            timeout.cancel()

    def test_connection_close(self):
        fd = self.connect().makefile(bufsize=1)
        fd.write('GET / HTTP/1.1\r\nHost: localhost\r\n\r\n')
        read_http(fd)
        fd.write('GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
        read_http(fd)
        fd.write('GET / HTTP/1.1\r\nHost: localhost\r\n\r\n')
        try:
            result = fd.readline()
            assert not result, 'The remote side is expected to close the connection, but it send %r' % (result, )
        except socket.error, ex:
            if ex[0] not in CONN_ABORTED_ERRORS:
                raise

    def SKIP_test_006_reject_long_urls(self):
        fd = self.connect().makefile(bufsize=1)
        path_parts = []
        for ii in range(3000):
            path_parts.append('path')
        path = '/'.join(path_parts)
        request = 'GET /%s HTTP/1.0\r\nHost: localhost\r\n\r\n' % path
        fd.write(request)
        result = fd.readline()
        status = result.split(' ')[1]
        self.assertEqual(status, '414')
        fd.close()


class TestNoChunks(CommonTests):
    # when returning a list of strings a shortcut is employed by the server:
    # it calculates the content-length and joins all the chunks before sending
    validator = None

    @staticmethod
    def application(env, start_response):
        path = env['PATH_INFO']
        if path == '/':
            start_response('200 OK', [('Content-Type', 'text/plain')])
            return ['hello ', 'world']
        else:
            start_response('404 Not Found', [('Content-Type', 'text/plain')])
            return ['not ', 'found']

    def test(self):
        fd = self.connect().makefile(bufsize=1)
        fd.write('GET / HTTP/1.1\r\nHost: localhost\r\n\r\n')
        response = read_http(fd, body='hello world')
        assert response.chunks is None, response.chunks
        response.assertHeader('Content-Length', '11')

        fd.write('GET /not-found HTTP/1.1\r\nHost: localhost\r\n\r\n')
        response = read_http(fd, code=404, reason='Not Found', body='not found')
        assert response.chunks is None, response.chunks
        response.assertHeader('Content-Length', '9')


class TestExplicitContentLength(TestNoChunks):
    # when returning a list of strings a shortcut is empoyed by the server - it caculates the content-length

    @staticmethod
    def application(env, start_response):
        path = env['PATH_INFO']
        if path == '/':
            start_response('200 OK', [('Content-Type', 'text/plain'), ('Content-Length', '11')])
            return ['hello ', 'world']
        else:
            start_response('404 Not Found', [('Content-Type', 'text/plain'), ('Content-Length', '9')])
            return ['not ', 'found']


class TestYield(CommonTests):

    @staticmethod
    def application(env, start_response):
        path = env['PATH_INFO']
        if path == '/':
            start_response('200 OK', [('Content-Type', 'text/plain')])
            yield "hello world"
        else:
            start_response('404 Not Found', [('Content-Type', 'text/plain')])
            yield "not found"


class TestGetArg(TestCase):

    @staticmethod
    def application(env, start_response):
        body = env['wsgi.input'].read()
        a = cgi.parse_qs(body).get('a', [1])[0]
        start_response('200 OK', [('Content-Type', 'text/plain')])
        return ['a is %s, body is %s' % (a, body)]

    def test_007_get_arg(self):
        # define a new handler that does a get_arg as well as a read_body
        fd = self.connect().makefile(bufsize=1)
        request = '\r\n'.join((
            'POST / HTTP/1.0',
            'Host: localhost',
            'Content-Length: 3',
            '',
            'a=a'))
        fd.write(request)

        # send some junk after the actual request
        fd.write('01234567890123456789')
        read_http(fd, version='1.0', body='a is a, body is a=a')
        fd.close()


class TestChunkedApp(TestCase):

    chunks = ['this', 'is', 'chunked']

    def body(self):
        return ''.join(self.chunks)

    def application(self, env, start_response):
        start_response('200 OK', [('Content-Type', 'text/plain')])
        for chunk in self.chunks:
            yield chunk

    def test_chunked_response(self):
        fd = self.connect().makefile(bufsize=1)
        fd.write('GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
        response = read_http(fd, body=self.body())
        if server_implements_chunked:
            self.assertEqual(response.headers.get('Transfer-Encoding'), 'chunked')
            self.assertEqual(response.chunks, self.chunks)

    def test_no_chunked_http_1_0(self):
        fd = self.connect().makefile(bufsize=1)
        fd.write('GET / HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n')
        response = read_http(fd, version='1.0')
        self.assertEqual(response.body, self.body())
        self.assertEqual(response.headers.get('Transfer-Encoding'), None)
        content_length = int(response.headers.get('Content-Length'))
        if content_length is not None:
            self.assertEqual(content_length, len(self.body()))


class TestBigChunks(TestChunkedApp):
    chunks = ['a' * 8192] * 3


class TestChunkedPost(TestCase):

    @staticmethod
    def application(env, start_response):
        start_response('200 OK', [('Content-Type', 'text/plain')])
        if env['PATH_INFO'] == '/a':
            data = env['wsgi.input'].read()
            return [data]
        elif env['PATH_INFO'] == '/b':
            return [x for x in iter(lambda: env['wsgi.input'].read(4096), '')]
        elif env['PATH_INFO'] == '/c':
            return [x for x in iter(lambda: env['wsgi.input'].read(1), '')]

    def test_014_chunked_post(self):
        fd = self.connect().makefile(bufsize=1)
        fd.write('POST /a HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n'
                 'Transfer-Encoding: chunked\r\n\r\n'
                 '2\r\noh\r\n4\r\n hai\r\n0\r\n\r\n')
        read_http(fd, body='oh hai')

        fd = self.connect().makefile(bufsize=1)
        fd.write('POST /b HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n'
                 'Transfer-Encoding: chunked\r\n\r\n'
                 '2\r\noh\r\n4\r\n hai\r\n0\r\n\r\n')
        read_http(fd, body='oh hai')

        fd = self.connect().makefile(bufsize=1)
        fd.write('POST /c HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n'
                 'Transfer-Encoding: chunked\r\n\r\n'
                 '2\r\noh\r\n4\r\n hai\r\n0\r\n\r\n')
        read_http(fd, body='oh hai')


class TestUseWrite(TestCase):

    body = 'abcde'

    def application(self, env, start_response):
        if env['PATH_INFO'] == '/explicit-content-length':
            write = start_response('200 OK', [('Content-Type', 'text/plain'),
                                              ('Content-Length', '5')])
            write(self.body)
        elif env['PATH_INFO'] == '/no-content-length':
            write = start_response('200 OK', [('Content-Type', 'text/plain')])
            write(self.body)
        else:
            raise Exception('Invalid url')
        return []

    def test_015_write(self):
        fd = self.connect().makefile(bufsize=1)
        fd.write('GET /explicit-content-length HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
        response = read_http(fd, body=self.body)
        response.assertHeader('Content-Length', '5')

        fd = self.connect().makefile(bufsize=1)
        fd.write('GET /no-content-length HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
        response = read_http(fd, body=self.body)
        if server_implements_chunked:
            response.assertHeader('Transfer-Encoding', 'chunked')


class TestHttps(TestCase):

    def setUp(self):
        certificate_file = os.path.join(os.path.dirname(__file__), 'test_server.crt')
        private_key_file = os.path.join(os.path.dirname(__file__), 'test_server.key')

        sock = socket.socket()
        socket.bind_and_listen(sock, ('', 4201))
        self.sock = socket.ssl(sock, private_key_file, certificate_file)

        self.g = gevent.spawn(self.get_wsgi_module().server, self.sock, validator(self.application))

    def tearDown(self):
        self.g.kill(block=True)

    def urlopen(self, *args, **kwargs):
        req = HTTPRequest("https://localhost:4201/foo", *args, **kwargs)
        return urllib2.urlopen(req)

    def application(self, environ, start_response):
        start_response('200 OK', [('Content-Type', 'text/plain')])
        return [environ['wsgi.input'].read()]

    def test_012_ssl_server(self):
        result = self.urlopen(method="POST", data='abc').read()
        self.assertEquals(result, 'abc')

    def test_013_empty_return(self):
        result = self.urlopen().read()
        self.assertEquals(result, '')


class TestInternational(TestCase):

    def application(self, environ, start_response):
        assert environ['PATH_INFO'] == '/\xd0\xbf\xd1\x80\xd0\xb8\xd0\xb2\xd0\xb5\xd1\x82', environ['PATH_INFO']
        assert environ['QUERY_STRING'] == '%D0%B2%D0%BE%D0%BF%D1%80%D0%BE%D1%81=%D0%BE%D1%82%D0%B2%D0%B5%D1%82', environ['QUERY_STRING']
        start_response("200 PASSED", [('Content-Type', 'text/plain')])
        return []

    def test(self):
        sock = self.connect()
        sock.sendall('GET /%D0%BF%D1%80%D0%B8%D0%B2%D0%B5%D1%82?%D0%B2%D0%BE%D0%BF%D1%80%D0%BE%D1%81=%D0%BE%D1%82%D0%B2%D0%B5%D1%82 HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
        read_http(sock.makefile(), reason='PASSED')


class TestInputReadline(TestCase):

    validator = None

    def application(self, environ, start_response):
        input = environ['wsgi.input']
        lines = []
        while True:
            line = input.readline()
            if not line:
                break
            lines.append(repr(line) + ' ')
        start_response('200 hello', [])
        return lines

    def test(self):
        fd = self.connect().makefile()
        content = 'hello\n\nworld\n123'
        fd.write('POST / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n'
                 'Content-Length: %s\r\n\r\n%s' % (len(content), content))
        fd.flush()
        read_http(fd, reason='hello', body="'hello\\n' '\\n' 'world\\n' '123' ")


class TestInputIter(TestInputReadline):

    def application(self, environ, start_response):
        input = environ['wsgi.input']
        lines = []
        for line in input:
            if not line:
                break
            lines.append(repr(line) + ' ')
        start_response('200 hello', [])
        return lines


class TestInputReadlines(TestInputReadline):

    def application(self, environ, start_response):
        input = environ['wsgi.input']
        lines = input.readlines()
        lines = [repr(line) + ' ' for line in lines]
        start_response('200 hello', [])
        return lines


class ExpectedException(Exception):
    pass


class TestError(TestCase):

    @staticmethod
    def application(env, start_response):
        raise ExpectedException

    @property
    def url(self):
        return 'http://127.0.0.1:%s' % self.port

    def test(self):
        try:
            r = urllib2.urlopen(self.url)
            raise AssertionError('Must raise HTTPError, returned %r: %s' % (r, r.code))
        except urllib2.HTTPError, ex:
            assert ex.code == 500, ex
            assert ex.msg == 'Internal Server Error', ex


class TestError_after_start_response(TestError):

    @staticmethod
    def application(env, start_response):
        start_response('200 OK', [('Content-Type', 'text/plain')])
        raise ExpectedException

class TestEmptyYield(TestCase):

    @staticmethod
    def application(env, start_response):
        start_response('200 OK', [('Content-Type', 'text/plain')])
        yield ""
        yield ""

    def test_err(self):
        fd = self.connect().makefile(bufsize=1)
        fd.write('GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
        st, h, body = read_http(fd)
        assert body==""

        garbage = fd.read()
        self.assert_(garbage=="", "got garbage: %r" % garbage)


class TestEmptyWrite(TestEmptyYield):
    @staticmethod
    def application(env, start_response):
        write = start_response('200 OK', [('Content-Type', 'text/plain')])
        write("")
        write("")
        return []


class HTTPRequest(urllib2.Request):
    """Hack urllib2.Request to support PUT and DELETE methods."""

    def __init__(self, url, method="GET", data=None, headers={},
                 origin_req_host=None, unverifiable=False):
        urllib2.Request.__init__(self,url,data,headers,origin_req_host,unverifiable)
        self.url = url
        self.method = method

    def get_method(self):
        return self.method

del CommonTests

if __name__ == '__main__':
    greentest.main()
