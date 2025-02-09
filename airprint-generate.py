#!/usr/bin/env python3

"""
Copyright (c) 2010 Timothy J Fontaine <tjfontaine@atxconsulting.com>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.

***
Discovery by DNS-SD: Copyright (c) 2013 Vidar Tysse <news@vidartysse.net>
***
***
Update for Secure IPPS/HTTPS Printing and CUPS version 2.1:
Copyright (c) 2016 Julian Pawlowski <julian.pawlowski@gmail.com>
***
"""

import os, optparse, re, uuid, pprint
import urllib.parse as urlparse
import os.path
from io import StringIO

from xml.dom.minidom import parseString
from xml.dom import minidom

import sys

try:
    import lxml.etree as etree
    from lxml.etree import Element, ElementTree, tostring
except:
    try:
        from xml.etree.ElementTree import Element, ElementTree, tostring
        etree = None
    except:
        try:
            from elementtree import Element, ElementTree, tostring
            etree = None
        except:
            raise 'Failed to find python libxml or elementtree, please install one of those or use python >= 2.5'

try:
    import cups
except:
    cups = None

try:
    import avahisearch
except:
    avahisearch = None

XML_TEMPLATE = """<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
<name replace-wildcards="yes"></name>
<service>
	<type>_ipp._tcp</type>
	<subtype>_universal._sub._ipp._tcp</subtype>
	<port>631</port>
	<txt-record>txtvers=1</txt-record>
	<txt-record>qtotal=1</txt-record>
	<txt-record>Transparent=T</txt-record>
	<txt-record>URF=DM3</txt-record>
</service>
</service-group>"""

#TODO XXX FIXME
#<txt-record>Bind=T</txt-record>

DOCUMENT_TYPES = {
    # These content-types will be at the front of the list
    'application/pdf': True,
    'application/postscript': True,
    'application/vnd.cups-postscript': True,
    'application/vnd.cups-raster': True,
    'application/octet-stream': True,
    'image/urf': True,
    'image/png': True,
    'image/tiff': True,
    'image/png': True,
    'image/jpeg': True,
    'image/gif': True,
    'text/plain': True,
    'text/html': True,

    # These content-types will never be reported
    'image/x-xwindowdump': False,
    'image/x-xpixmap': False,
    'image/x-xbitmap': False,
    'image/x-sun-raster': False,
    'image/x-sgi-rgb': False,
    'image/x-portable-pixmap': False,
    'image/x-portable-graymap': False,
    'image/x-portable-bitmap': False,
    'image/x-portable-anymap': False,
    'application/x-shell': False,
    'application/x-perl': False,
    'application/x-csource': False,
    'application/x-cshell': False,
}

class AirPrintGenerate(object):
    def __init__(self, host=None, user=None, port=None, verbose=False,
        directory=None, prefix='AirPrint-', adminurl=False, usecups=True,
        useavahi=False, dnsdomain=None, tlsversion=None):
        self.host = host
        self.user = user
        self.password = None
        self.port = port
        self.verbose = verbose
        self.directory = directory
        self.prefix = prefix
        self.adminurl = adminurl
        self.usecups = usecups and cups
        self.useavahi = useavahi and avahisearch
        self.dnsdomain = dnsdomain
        self.tlsversion = tlsversion
        
        if self.usecups and cups and self.user:
            cups.setUser(self.user)
            from getpass import getpass
            self.password = getpass()
            cups.setPasswordCB(self.get_password)

    def get_password(self):
        return self.password
    
    def generate(self):
        collected_printers = list()

        # Collect shared printers from CUPS if applicable
        if self.usecups and cups:
            if self.verbose:
                sys.stderr.write('Collecting shared printers from CUPS%s' % os.linesep)
            if not self.host:
                conn = cups.Connection()
            else:
                if not self.port:
                    self.port = 631
                conn = cups.Connection(self.host, self.port)

            printers = conn.getPrinters()

            for p, v in printers.items():
                if v['printer-is-shared']:
                    if self.verbose:
                     pprint.pprint(v)

                    attrs = conn.getPrinterAttributes(p)
                    uri = urlparse.urlparse(v['printer-uri-supported'])

                    port_no = None
                    if hasattr(uri, 'port'):
                      port_no = uri.port
                    if not port_no:
                        port_no = self.port
                    if not port_no:
                        port_no = cups.getPort()

                    if hasattr(uri, 'path'):
                      rp = uri.path
                    else:
                      rp = uri[2]
                    re_match = re.match(r'^//(.*):(\d+)(/.*)', rp)
                    if re_match:
                      rp = re_match.group(3)
                    #Remove leading slashes from path
                    #TODO XXX FIXME I'm worried this will match broken urlparse
                    #results as well (for instance if they don't include a port)
                    #the xml would be malform'd either way
                    rp = re.sub(r'^/+', '', rp)

                    pdl = Element('txt-record')
                    fmts = []
                    defer = []

                    for a in attrs['document-format-supported']:
                        if a in DOCUMENT_TYPES:
                            if DOCUMENT_TYPES[a]:
                                fmts.append(a)
                        else:
                            defer.append(a)

                    if 'image/urf' not in fmts:
                        sys.stderr.write('image/urf is not in mime types, %s may not be available on ios6 (see https://github.com/tjfontaine/airprint-generate/issues/5)%s' % (p, os.linesep))

                    fmts = ','.join(fmts+defer)

                    dropped = []

                    # TODO XXX FIXME all fields should be checked for 255 limit
                    while len('pdl=%s' % (fmts)) >= 255:
                        (fmts, drop) = fmts.rsplit(',', 1)
                        dropped.append(drop)

                    if len(dropped) and self.verbose:
                        sys.stderr.write('%s Losing support for: %s%s' % (p, ','.join(dropped), os.linesep))

                    air_setting = 'none'
                    if self.user is not None and self.password is not None:
                        air_setting = '%s,%s' % (self.user, self.password)

                    binary_setting = 'F'
                    if 'charset-supported' in attrs:
                        for charset in attrs['charset-supported']:
                            if charset == 'utf-8':
                                binary_setting = 'T'

                    collate_setting = 'F'
                    if 'multiple-document-handling-supported' in attrs:
                        for doc_handling_option in attrs['multiple-document-handling-supported']:
                            if doc_handling_option == 'separate-documents-collated-copies':
                                collate_setting = 'T'

                    color_setting = 'F'
                    if 'color-supported' in attrs and attrs['color-supported'] == True:
                        color_setting = 'T'

                    copies_setting = 'F'
                    if 'copies-supported' in attrs:
                        for copies in attrs['copies-supported']:
                            if int(copies) > 1:
                                copies_setting = 'T'

                    duplex_setting = 'F'
                    if 'sides-supported' in attrs and len(attrs['sides-supported']) > 1:
                        duplex_setting = 'T'

                    tbcp_setting = 'F'
                    if 'port-monitor-supported' in attrs:
                        for port_monitor in attrs['port-monitor-supported']:
                            if port_monitor == 'tbcp':
                                tbcp_setting = 'T'

                    usbmdl_setting = ''
                    usbmfg_setting = ''
                    if 'printer-device-id' in attrs:
                        device_id_split = attrs['printer-device-id'].split(";")
                        for device_id_entry in device_id_split:
                            if len(device_id_entry) > 0:
                                device_id_details = device_id_entry.split(":")
                                if device_id_details[0] == 'MDL':
                                    usbmdl_setting = device_id_details[1]
                                elif device_id_details[0] == 'MFG':
                                    usbmfg_setting = device_id_details[1]

                    collected_printers.append( {
                        'SOURCE'    : 'CUPS', 
                        'name'      : p, 
                        'host'      : None,     # Could/should use self.host, but would break old behaviour
                        'address'   : None,
                        'port'      : port_no,
                        'domain'    : 'local', 
                        'txt'       : {
                            'air'           : air_setting,
                            'rp'            : rp,
                            'note'          : v['printer-location'],
                            'product'       : '(%s)' % (v['printer-make-and-model']),
                            'Binary'        : binary_setting,
                            'Collate'       : collate_setting,
                            'Color'         : color_setting,
                            'Copies'        : copies_setting,
                            'Duplex'        : duplex_setting,
                            'TBCP'          : tbcp_setting,
                            'ty'            : v['printer-info'],
                            'printer-state' : v['printer-state'],
                            'printer-type'  : hex(v['printer-type']),
                            'usb_MDL'       : usbmdl_setting,
                            'usb_MFG'       : usbmfg_setting,
                            'adminurl'      : v['printer-uri-supported'],
                            'UUID'          : str(uuid.uuid4()),
                            'pdl'           : fmts,
                            }
                        } )

        # Collect networked printers using DNS-SD if applicable
        if (self.useavahi):
            if self.verbose:
                sys.stderr.write('Collecting networked printers using DNS-SD%s' % os.linesep)
            finder = avahisearch.AvahiPrinterFinder(verbose=self.verbose)
            for p in finder.Search():
                p['SOURCE'] = 'DNS-SD'
                collected_printers.append(p)

        # Produce a .service file for each printer found
        for p in collected_printers:
            self.produce_settings_file(p)

    def produce_settings_file(self, printer):
        printer_name = printer['name']

        tree = ElementTree()
        tree.parse(StringIO(XML_TEMPLATE.replace('\n', '').replace('\r', '').replace('\t', '')))

        name_node = tree.find('name')
        if self.tlsversion is not None:
            name_node.text = 'Sec.AirPrint %s @ %%h' % printer_name
        else:
            name_node.text = 'AirPrint %s @ %%h' % printer_name

        service_node = tree.find('service')

        port_node = service_node.find('port')
        port_node.text = '%d' % printer['port']

        if self.tlsversion is not None:
            type_node = service_node.find('type')
            type_node.text = '_ipps._tcp'
            subtype_node = service_node.find('subtype')
            subtype_node.text = '_universal._sub._ipps._tcp'
            txt_tls_node = Element('txt-record')
            txt_tls_node.text = 'TLS=%s' % (self.tlsversion)
            service_node.append(txt_tls_node)

        host = printer['host']
        if host:
            if self.dnsdomain:
                pair = host.rsplit('.', 1)
                if len(pair) > 1:
                    host = '.'.join((pair[0], self.dnsdomain))
            service_node.append(self.new_node('host-name', host))

        txt = printer['txt']
        for key in txt:
            if self.adminurl or key != 'adminurl':
                service_node.append(self.new_txtrecord_node('%s=%s' % (key, txt[key])))

        source = printer['SOURCE'] if 'SOURCE' in printer else ''

        fname = '%s%s%s.service' % (self.prefix, '%s-' % source if len(source) > 0 else '', printer_name)

        if self.directory:
            fname = os.path.join(self.directory, fname)

        f = open(fname, 'wb' if etree else 'w')

        if etree:
            tree.write(fname, pretty_print=True, xml_declaration=True, encoding="UTF-8")
        else:
            xmlstr = tostring(tree.getroot())
            doc = parseString(xmlstr)
            dt= minidom.getDOMImplementation('').createDocumentType('service-group', None, 'avahi-service.dtd')
            doc.insertBefore(dt, doc.documentElement)
            doc.writexml(f)
        f.close()

        if self.verbose:
            src = source if len(source) > 0 else 'unknown'
            sys.stderr.write('Created from %s: %s%s' % (src, fname, os.linesep))

    def new_txtrecord_node(self, text):
        return self.new_node('txt-record', text)

    def new_node(self, tag, text):
        element = Element(tag)
        element.text = text
        return element

if __name__ == '__main__':
    parser = optparse.OptionParser()
    parser.add_option('-s', '--dnssd', action="store_true", dest="avahi",
        help="Search for network printers using DNS-SD (requires avahi)")
    parser.add_option('-D', '--dnsdomain', action="store", type="string",
        dest='dnsdomain', help='DNS domain where printers are located.',
        metavar='DNSDOMAIN')
    parser.add_option('-c', '--cups', action="store_true", dest="cups",
        help="Search CUPS for shared printers (requires CUPS)")
    parser.add_option('-t', '--tls-version', action="store", dest="tlsversion",
        help="Use the specified TLS version for secure printing connections")
    parser.add_option('-H', '--host', action="store", type="string",
        dest='hostname', help='Hostname of CUPS server (optional)', metavar='HOSTNAME')
    parser.add_option('-P', '--port', action="store", type="int",
        dest='port', help='Port number of CUPS server', metavar='PORT')
    parser.add_option('-u', '--user', action="store", type="string",
        dest='username', help='Username to authenticate with against CUPS',
        metavar='USER')
    parser.add_option('-d', '--directory', action="store", type="string",
        dest='directory', help='Directory to create service files',
        metavar='DIRECTORY')
    parser.add_option('-v', '--verbose', action="store_true", dest="verbose",
        help="Print debugging information to STDERR")
    parser.add_option('-p', '--prefix', action="store", type="string",
        dest='prefix', help='Prefix all files with this string', metavar='PREFIX',
        default='AirPrint-')
    parser.add_option('-a', '--admin', action="store_true", dest="adminurl",
        help="Include the printer specified uri as the adminurl")
    
    (options, args) = parser.parse_args()
    
    if options.cups and not cups:
        sys.stderr.write('Warning: CUPS is not available. Ignoring --cups option.%s' % os.linesep)
    if options.avahi and not avahisearch:
        sys.stderr.write('Warning: Module avahisearch is not available. Ignoring --dnssd option.%s' % os.linesep)
    if not (options.cups and cups) and not (options.avahi and avahisearch):
        sys.stderr.write('Nothing do do: --cups and/or --dnssd must be specified, and CUPS and/or avahi must be installed.%s' % os.linesep)
        os._exit(1)

    if options.directory:
        if not os.path.exists(options.directory):
            os.mkdir(options.directory)
    
    apg = AirPrintGenerate(
        user=options.username,
        host=options.hostname,
        port=options.port,
        verbose=options.verbose,
        directory=options.directory,
        prefix=options.prefix,
        adminurl=options.adminurl,
        usecups=options.cups,
        useavahi=options.avahi,
        dnsdomain=options.dnsdomain,
        tlsversion=options.tlsversion
    )
    
    apg.generate()

    if options.avahi and avahisearch and not options.dnsdomain:
        sys.stderr.write("NOTE: If a printer found by DNS-SD does not resolve outside the local subnet, specify the printer's DNS domain with --dnsdomain or edit the generated <host-name> element to fit your network.%s" % os.linesep)
