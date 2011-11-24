# -*- coding: utf-8 -*-
import os
import thread
import time
import codecs
import json
from PyQt4 import QtCore, QtWebKit
from PyQt4.QtNetwork import QNetworkRequest


class CasperWebPage(QtWebKit.QWebPage):
    """Overrides QtWebKit.QWebPage."""
    def javaScriptConsoleMessage(self, message, *args, **kwargs):
        """Prints client console message in current output stream."""
        super(CasperWebPage, self).javaScriptConsoleMessage(message, *args,
            **kwargs)
        print "\033[92mJavascript console: \033[0m%s" % message


def client_utils_required(func):
    """Decorator that checks avabality of Capser client side utils,
    injects require javascript file instead.
    """
    def wrapper(self, *args):
        if self.evaluate('CasperUtils;').type() == 0:
            self.evaluate(codecs.open('utils.js').read())
        return func(self, *args)
    return wrapper


class HttpRessource(object):
    """Represents an HTTP ressource.
    """
    def __init__(self, reply):
        self.url = unicode(reply.request().url().toString())
        self.http_status = reply.attribute(
            QNetworkRequest.HttpStatusCodeAttribute).toInt()[0]
        self._reply = reply


class Casper(object):
    """Casper manage a QtApplication executed on its own thread.

    :param wait_timeout: Maximum step duration.
    """
    lock = None
    command = None
    retval = None

    def __init__(self, wait_timeout=5):
        self.http_ressources = []

        self.wait_timeout = wait_timeout

        if not Casper.lock:
            Casper.lock = thread.allocate_lock()

            # To Qt thread pipe
            Casper.pipetoveusz_r, w = os.pipe()
            Casper.pipetoveusz_w = os.fdopen(w, 'w', 0)

            # Return pipe
            r, w = os.pipe()
            Casper.pipefromveusz_r = os.fdopen(r, 'r', 0)
            Casper.pipefromveusz_w = os.fdopen(w, 'w', 0)

            thread.start_new_thread(Casper._start, (self,))
            # As there's no callback on application started,
            # lets leep for a while...
            # TODO: fix this
            time.sleep(0.5)

    @client_utils_required
    def click(self, selector):
        """Click the targeted element.

        :param selector: A CSS3 selector to targeted element.
        """
        return self.evaluate('CasperUtils.click("%s");' % selector)

    @property
    def content(self):
        """Gets current frame HTML as a string."""
        return unicode(self.main_frame.toHtml())

    def evaluate(self, script, releasable=True):
        """Evaluates script in page frame.

        :param script: The script to evaluate.
        :param releasable: Specifies if callback waiting is needed.
        """
        return self._run(
                lambda self, script: self.main_frame\
                    .evaluateJavaScript("%s" % script),
                releasable, *(self, script)
            )

    @client_utils_required
    def fill(self, selector, values, submit=True):
        """Fills a form with provided values.

        :param selector: A CSS selector to the target form to fill.
        :param values: A dict containing the values.
        :param submit: A boolean that force form submition.
        """
        # self.evaluate('CasperUtils.fill("toto", "titi");')
        return self.evaluate('CasperUtils.fill("%s", %s);' % (
            selector, unicode(json.dumps(values))))

    def open(self, address, method='get'):
        """Opens a web ressource.

        :param address: The ressource URL.
        :param method: The Http method.
        :return: All loaded ressources.
        """
        def open_ressource(self, address, method):
            from PyQt4 import QtCore
            from PyQt4.QtNetwork import QNetworkAccessManager, QNetworkRequest
            body = QtCore.QByteArray()
            try:
                method = getattr(QNetworkAccessManager,
                    "%sOperation" % method.capitalize())
            except AttributeError:
                raise Exception("Invalid http method %s" % method)
            self.main_frame.load(QNetworkRequest(QtCore.QUrl(address)),
                method, body)
            return self.page

        return self._run(open_ressource, False, *(self, address, method))

    def wait_for_selector(self, selector):
        """Waits until selector match an element on the frame.

        :param selector: The selector to wait for.
        """
        while self.evaluate('document.querySelector("%s");' % selector)\
            .type() == 10:
            time.sleep(0.1)
        return self._release_last_ressources()

    def wait_for_text(self, text):
        """Waits until given text appear on main frame.

        :param text: The text to wait for.
        """
        while text not in self.content:
            time.sleep(0.1)
        return self._release_last_ressources()

    def _run(self, cmd, releasable, *args, **kwargs):
        """Execute the given command in the Qt thread.

        :param cmd: The command to execute.
        :param releasable: Specifies if callback waiting is needed.
        """
        assert Casper.command == None and Casper.retval == None
        # Sends the command to Qt thread
        Casper.lock.acquire()
        Casper.command = (cmd, releasable, args, kwargs)
        Casper.lock.release()
        Casper.pipetoveusz_w.write('N')
        # Waits for command to be executed
        Casper.pipefromveusz_r.read(1)
        Casper.lock.acquire()
        retval = Casper.retval
        Casper.command = None
        Casper.retval = None
        Casper.lock.release()
        if isinstance(retval, Exception):
            raise retval
        else:
            return retval

    def _start(self):
        """Starts a QtApplication on the dedicated thread.

        :note: Imports have to be done inside thread.
        """
        from PyQt4 import QtCore
        from PyQt4 import QtGui
        from PyQt4 import QtWebKit

        class CasperApp(QtGui.QApplication):
            def notification(self, i):
                """Notifies application from main thread calls.
                """
                Casper.lock.acquire()
                os.read(Casper.pipetoveusz_r, 1)

                assert Casper.command is not None
                cmd, releasable, args, kwargs = Casper.command
                try:
                    Casper.retval = cmd(*args, **kwargs)
                except Exception, e:
                    Casper.retval = e

                if releasable:
                    Casper._release()

        app = CasperApp(['casper'])
        notifier = QtCore.QSocketNotifier(Casper.pipetoveusz_r,
                                       QtCore.QSocketNotifier.Read)
        app.connect(notifier, QtCore.SIGNAL('activated(int)'),
            app.notification)
        notifier.setEnabled(True)

        self.page = CasperWebPage(app)
        self.page.setViewportSize(QtCore.QSize(400, 300))

        self.page.loadFinished.connect(self._page_loaded)
        self.page.networkAccessManager().finished.connect(self._request_ended)

        self.main_frame = self.page.mainFrame()

        app.exec_()

    def _release_last_ressources(self):
        """Releases last loaded ressources.

        :return: The released ressources.
        """
        last_ressources = self.http_ressources
        self.http_ressources = []
        return last_ressources

    def _page_loaded(self):
        """Call back main thread when page loaded.
        """
        Casper.retval = self._release_last_ressources()
        Casper._release()

    @staticmethod
    def _release():
        """Releases the back pipe."""
        Casper.lock.release()
        Casper.pipefromveusz_w.write('r')

    def _request_ended(self, res):
        """Adds an HttpRessource object to http_ressources.

        :param res: The request result.
        """
        self.http_ressources.append(HttpRessource(res))
