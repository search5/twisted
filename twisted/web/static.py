# -*- test-case-name: twisted.test.test_web -*-
# Twisted, the Framework of Your Internet
# Copyright (C) 2001 Matthew W. Lefkowitz
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of version 2.1 of the GNU Lesser General Public
# License as published by the Free Software Foundation.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

"""I deal with static resources.
"""

from __future__ import nested_scopes

# System Imports
import os, stat, string
import cStringIO
import traceback
import warnings
import types
StringIO = cStringIO
del cStringIO
import urllib

# Sibling Imports
import server
import error
import resource
from twisted.web.util import redirectTo

# Twisted Imports
from twisted.protocols import http
from twisted.python import threadable, log, components, failure, filepath
from twisted.internet import abstract, interfaces, defer
from twisted.spread import pb
from twisted.persisted import styles
from twisted.python.util import InsensitiveDict
from twisted.python.runtime import platformType


dangerousPathError = error.NoResource("Invalid request URL.")

def isDangerous(path):
    return path == '..' or '/' in path or os.sep in path


class Data(resource.Resource):
    """
    This is a static, in-memory resource.
    """

    def __init__(self, data, type):
        resource.Resource.__init__(self)
        self.data = data
        self.type = type

    def render(self, request):
        request.setHeader("content-type", self.type)
        if request.method == "HEAD":
            request.setHeader("content-length", len(self.data))
            return ''
        return self.data

def addSlash(request):
    return "http%s://%s%s/" % (
        request.isSecure() and 's' or '',
        request.getHeader("host"),
        (string.split(request.uri,'?')[0]))

class Redirect(resource.Resource):
    def __init__(self, request):
        resource.Resource.__init__(self)
        self.url = addSlash(request)

    def render(self, request):
        return redirectTo(self.url, request)

from twisted.internet.interfaces import IServiceCollection
from twisted.internet.app import ApplicationService

class Registry(components.Componentized, styles.Versioned):
    """
    I am a Componentized object that will be made available to internal Twisted
    file-based dynamic web content such as .rpy and .epy scripts.
    """

    def __init__(self):
        components.Componentized.__init__(self)
        self._pathCache = {}

    persistenceVersion = 1

    def upgradeToVersion1(self):
        self._pathCache = {}

    def cachePath(self, path, rsrc):
        self._pathCache[path] = rsrc

    def getCachedPath(self, path):
        return self._pathCache.get(path)

    def _grabService(self, svc, sclas):
        """
        Find an instance of a particular class in a service collection and all
        subcollections.
        """
        for s in svc.services.values():
            if isinstance(s, sclas):
                return s
            if components.implements(s, IServiceCollection):
                ss = self._grabService(s, sclas)
                if ss:
                    return ss

    def getComponent(self, interface, registry=None):
        """
        Very similar to Componentized.getComponent, with a little magic.

        This adds the additional default behavior that if no component already
        exists and 'interface' is a subclass of
        L{twisted.internet.app.ApplicationService}, it will automatically scan
        through twisted.internet.app.theApplication and look for instances of
        'interface'.

        This has the general effect that if your web script (in an RPY, EPY, or
        anywhere else that a Registry is present) wishes to locate a Service in
        a default webserver, it can say 'registry.getComponent(MyServiceClass)'
        and if there is a service of that type registered with the Application,
        it will be found.  Additionally, in a more complex server, the registry
        can be explicitly given a service to locate for that interface using
        setComponent(MyServiceClass, myServiceInstance). Separate File
        instances can be used to represent access to different services.
        """
        c = components.Componentized.getComponent(self, interface, registry)
        if c is not None:
            return c
        elif issubclass(interface, ApplicationService):
            from twisted.internet.app import theApplication
            gs = self._grabService(theApplication, interface)
            if gs:
                self.setComponent(interface, gs)
                return gs


def _upgradeRegistry(registry):
    from twisted.internet import app
    registry.setComponent(interfaces.IServiceCollection,
                          app.theApplication)


def loadMimeTypes(mimetype_locations=['/etc/mime.types']):
    """
    Multiple file locations containing mime-types can be passed as a list.
    The files will be sourced in that order, overriding mime-types from the
    files sourced beforehand, but only if a new entry explicitly overrides
    the current entry.
    """
    import mimetypes
    # Grab Python's built-in mimetypes dictionary.
    contentTypes = mimetypes.types_map
    # Update Python's semi-erroneous dictionary with a few of the
    # usual suspects.
    contentTypes.update(
        {
            '.conf':  'text/plain',
            '.diff':  'text/plain',
            '.exe':   'application/x-executable',
            '.flac':  'audio/x-flac',
            '.java':  'text/plain',
            '.ogg':   'application/ogg',
            '.oz':    'text/x-oz',
            '.swf':   'application/x-shockwave-flash',
            '.tgz':   'application/x-gtar',
            '.wml':   'text/vnd.wap.wml',
            '.xul':   'application/vnd.mozilla.xul+xml',
        }
    )
    # Users can override these mime-types by loading them out configuration
    # files (this defaults to ['/etc/mime.types']).
    for location in mimetype_locations:
        if os.path.exists(location):
            contentTypes.update(mimetypes.read_mime_types(location))
            
    return contentTypes

def getTypeAndEncoding(filename, types, encodings, defaultType):
    p, ext = os.path.splitext(filename)
    ext = ext.lower()
    if encodings.has_key(ext):
        enc = encodings[ext]
        ext = os.path.splitext(p)[1].lower()
    else:
        enc = None
    type = types.get(ext, defaultType)
    return type, enc

class File(resource.Resource, styles.Versioned, filepath.FilePath):
    """
    File is a resource that represents a plain non-interpreted file
    (although it can look for an extension like .rpy or .cgi and hand the
    file to a processor for interpretation if you wish). Its constructor
    takes a file path.

    Alternatively, you can give a directory path to the constructor. In this
    case the resource will represent that directory, and its children will
    be files underneath that directory. This provides access to an entire
    filesystem tree with a single Resource.

    If you map the URL 'http://server/FILE' to a resource created as
    File('/tmp'), then http://server/FILE/ will return an HTML-formatted
    listing of the /tmp/ directory, and http://server/FILE/foo/bar.html will
    return the contents of /tmp/foo/bar.html .

    @cvar childNotFound: L{Resource} used to render 404 Not Found error pages.
    """

    # we don't implement IConfigCollection
    __implements__ = resource.IResource

    contentTypes = loadMimeTypes()

    contentEncodings = {
        ".gz" : "application/x-gzip",
        ".bz2": "application/x-bzip2"
        }

    processors = {}

    indexNames = ["index", "index.html", "index.htm", "index.trp", "index.rpy"]

    type = None

    ### Versioning

    persistenceVersion = 6

    def upgradeToVersion6(self):
        self.ignoredExts = []
        if self.allowExt:
            self.ignoreExt("*")
        del self.allowExt

    def upgradeToVersion5(self):
        if not isinstance(self.registry, Registry):
            self.registry = Registry()
            from twisted.internet import reactor
            reactor.callLater(0, _upgradeRegistry, self.registry)

    def upgradeToVersion4(self):
        if not hasattr(self, 'registry'):
            self.registry = {}

    def upgradeToVersion3(self):
        if not hasattr(self, 'allowExt'):
            self.allowExt = 0

    def upgradeToVersion2(self):
        self.defaultType = "text/html"

    def upgradeToVersion1(self):
        if hasattr(self, 'indexName'):
            self.indexNames = [self.indexName]
            del self.indexName

    def __init__(self, path, defaultType="text/html", ignoredExts=(), registry=None, allowExt=0):
        """Create a file with the given path.
        """
        resource.Resource.__init__(self)
        filepath.FilePath.__init__(self, path)
        # Remove the dots from the path to split
        self.defaultType = defaultType
        if ignoredExts in (0, 1) or allowExt:
            warnings.warn("ignoredExts should receive a list, not a boolean")
            if ignoredExts or allowExt:
                self.ignoredExts = ['*']
            else:
                self.ignoredExts = []
        else:
            self.ignoredExts = list(ignoredExts)
        self.registry = registry or Registry()

    def ignoreExt(self, ext):
        """Ignore the given extension.

        Serve file.ext if file is requested
        """
        self.ignoredExts.append(ext)

    childNotFound = error.NoResource("File not found.")

    def directoryListing(self):
        from twisted.web.woven import dirlist
        return dirlist.DirectoryLister(self.path,
                                       self.listNames(),
                                       self.contentTypes,
                                       self.contentEncodings,
                                       self.defaultType)

    def getChild(self, path, request):
        """See twisted.web.Resource.getChild.
        """
        self.restat()
        
        if not self.isdir():
            return self.childNotFound

        if path:
            fpath = self.child(path)
        else:
            fpath = self.childSearchPreauth(*self.indexNames)
            if fpath is None:
                return self.directoryListing()

        if not fpath.exists():
            searchNames = [(path + ext) for ext in self.ignoredExts]
            fpath = self.childSearchPreauth(*searchNames)
            if fpath is None:
                return self.childNotFound

        if platformType == "win32":
            # don't want .RPY to be different than .rpy, since that would allow
            # source disclosure.
            processor = InsensitiveDict(self.processors).get(fpath.splitext()[1])
        else:
            processor = self.processors.get(fpath.splitext()[1])
        if processor:
            return resource.IResource(processor(fpath.path, self.registry))
        return self.createSimilarFile(fpath.path)

    # methods to allow subclasses to e.g. decrypt files on the fly:
    def openForReading(self):
        """Open a file and return it."""
        return self.open()

    def getFileSize(self):
        """Return file size."""
        return self.getsize()


    def render(self, request):
        """You know what you doing."""
        self.restat()

        if self.type is None:
            self.type, self.encoding = getTypeAndEncoding(self.basename(),
                                                          self.contentTypes,
                                                          self.contentEncodings,
                                                          self.defaultType)

        if not self.exists():
            return error.NoResource("File not found.").render(request)

        if self.isdir():
            return self.redirect(request)

        #for content-length
        fsize = size = self.getFileSize()

        request.setHeader('accept-ranges','bytes')

        if self.type:
            request.setHeader('content-type', self.type)
        if self.encoding:
            request.setHeader('content-encoding', self.encoding)

        try:
            f = self.openForReading()
        except IOError, e:
            import errno
            if e[0] == errno.EACCES:
                return error.ForbiddenResource().render(request)
            else:
                raise

        if request.setLastModified(self.getmtime()) is http.CACHED:
            return ''

        try:
            range = request.getHeader('range')

            if range is not None:
                # This is a request for partial data...
                bytesrange = string.split(range, '=')
                assert bytesrange[0] == 'bytes',\
                       "Syntactically invalid http range header!"
                start, end = string.split(bytesrange[1],'-')
                if start:
                    f.seek(int(start))
                if end:
                    end = int(end)
                    size = end
                else:
                    end = size
                request.setResponseCode(http.PARTIAL_CONTENT)
                request.setHeader('content-range',"bytes %s-%s/%s " % (
                    str(start), str(end), str(size)))
                #content-length should be the actual size of the stuff we're
                #sending, not the full size of the on-server entity.
                fsize = end - int(start)

            request.setHeader('content-length', str(fsize))
        except:
            traceback.print_exc(file=log.logfile)

        if request.method == 'HEAD':
            return ''

        # return data
        FileTransfer(f, size, request)
        # and make sure the connection doesn't get closed
        return server.NOT_DONE_YET

    def redirect(self, request):
        return redirectTo(addSlash(request), request)

    def listNames(self):
        if not self.isdir():
            return []
        directory = self.listdir()
        directory.sort()
        return directory

    def listEntities(self):
        return map(lambda fileName, self=self: self.createSimilarFile(os.path.join(self.path, fileName)), self.listNames())

    def createPickleChild(self, name, child):
        if not os.path.isdir(self.path):
            resource.Resource.putChild(self, name, child)
        # xxx use a file-extension-to-save-function dictionary instead
        if type(child) == type(""):
            fl = open(os.path.join(self.path, name), 'wb')
            fl.write(child)
        else:
            if '.' not in name:
                name = name + '.trp'
            fl = open(os.path.join(self.path, name), 'wb')
            from pickle import Pickler
            pk = Pickler(fl)
            pk.dump(child)
        fl.close()

    def createSimilarFile(self, path):
        f = self.__class__(path, self.defaultType, self.ignoredExts, self.registry)
        # refactoring by steps, here - constructor should almost certainly take these
        f.processors = self.processors
        f.indexNames = self.indexNames[:]
        return f


import widgets

class DirectoryListing(widgets.StreamWidget, styles.Versioned):
    """DEPRECATED."""
    
    persistenceVersion = 1

    def upgradeToVersion1(self):
        self.contentTypes = File.contentTypes
        self.contentEncodings = File.contentEncodings
        self.defaultType = 'text/html'
    
    def __init__(self, pathname, dirs=None,
                 contentTypes=File.contentTypes,
                 contentEncodings=File.contentEncodings,
                 defaultType='text/html'):
        import warnings
        warnings.warn("Please use twisted.web.woven.dirlist", DeprecationWarning)
        self.contentTypes = contentTypes
        self.contentEncodings = contentEncodings
        self.defaultType = defaultType
        # dirs allows usage of the File to specify what gets listed
        self.dirs = dirs
        self.path = pathname

    def getTitle(self, request):
        return "Directory Listing For %s" % request.path

    def stream(self, write, request):
        if self.dirs is None:
            directory = os.listdir(self.path)
            directory.sort()
        else:
            directory = self.dirs

        write("<table><tr><th>Filename</th><th>Content type</th><th>Content encoding</th></tr>\n")

        for path in directory:
            url = urllib.quote(path, "/:")
            if os.path.isdir(os.path.join(self.path, path)):
                url = url + '/'
                write('<tr><td><a href="%s">%s/</a></td><td>[Directory]</td><td></td></tr>'
                      % (url, path))

        for path in directory:
            url = urllib.quote(path, "/:")
            if not os.path.isdir(os.path.join(self.path, path)):
                mimetype, encoding = getTypeAndEncoding(path, self.contentTypes, self.contentEncodings, self.defaultType)
                write('<tr><td><a href="%(url)s">%(name)s</a></td><td>[%(type)s]</td><td>%(encoding)s</tr>'
                      % {'url': url,
                         'name': path,
                         'type': mimetype,
                         'encoding': (encoding and '[%s]' % encoding or '')})
        write("</table>\n")

    def __repr__(self):
        return '<DirectoryListing of %r>' % self.path

    def __str__(self):
        return repr(self)


class FileTransfer(pb.Viewable):
    """
    A class to represent the transfer of a file over the network.
    """
    request = None
    def __init__(self, file, size, request):
        self.file = file
        self.size = size
        self.request = request
        request.registerProducer(self, 0)

    def resumeProducing(self):
        if not self.request:
            return
        self.request.write(self.file.read(abstract.FileDescriptor.bufferSize))
        if self.file.tell() == self.size:
            self.request.unregisterProducer()
            self.request.finish()
            self.request = None

    def pauseProducing(self):
        pass

    def stopProducing(self):
        self.file.close()
        self.request = None

    # Remotely relay producer interface.

    def view_resumeProducing(self, issuer):
        self.resumeProducing()

    def view_pauseProducing(self, issuer):
        self.pauseProducing()

    def view_stopProducing(self, issuer):
        self.stopProducing()


    synchronized = ['resumeProducing', 'stopProducing']

threadable.synchronize(FileTransfer)

"""I contain AsIsProcessor, which serves files 'As Is'
   Inspired by Apache's mod_asis
"""

class ASISProcessor(resource.Resource):

    def __init__(self, path, registry=None):
        resource.Resource.__init__(self)
        self.path = path
        self.registry = registry or static.Registry()

    def render(self, request):
        request.startedWriting = 1
        res = static.File(self.path, self.registry)
        return res.render(request)
