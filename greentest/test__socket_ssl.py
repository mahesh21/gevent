#!/usr/bin/python
from gevent import monkey; monkey.patch_all()

import unittest
import httplib
import socket

class AmazonHTTPSTests(unittest.TestCase):

    def test_amazon_response(self):
        conn = httplib.HTTPConnection('sdb.amazonaws.com')
        conn.debuglevel = 1
        conn.request('GET', '/')
        resp = conn.getresponse()

    def test_str_and_repr(self):
        conn = socket.socket()
        conn.connect(('sdb.amazonaws.com', 443))
        ssl_conn = socket.ssl(conn)
        assert str(ssl_conn)
        assert repr(ssl_conn)


if __name__ == "__main__":
    unittest.main()

