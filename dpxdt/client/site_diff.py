#!/usr/bin/env python
# Copyright 2013 Brett Slatkin
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Utility for doing incremental diffs for a live website."""

import HTMLParser
import Queue
import datetime
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import urlparse

# Local Libraries
import gflags
FLAGS = gflags.FLAGS

# Local modules
import capture_worker
import dpxdt
import pdiff_worker
import release_worker
import workers


gflags.DEFINE_integer(
    'crawl_depth', -1,
    'How deep to crawl. Depth of 0 means only the given page. 1 means pages '
    'that are one click away, 2 means two clicks, and so on. Set to -1 to '
    'scan every URL with the supplied prefix.')

gflags.DEFINE_spaceseplist(
    'ignore_prefixes', [],
    'URL prefixes that should not be crawled.')

gflags.DEFINE_string(
    'upload_build_id', None,
    'ID of the build to upload this screenshot set to as a new release.')

gflags.DEFINE_string(
    'upload_release_name', None,
    'Along with upload_build_id, the name of the release to upload to. If '
    'not supplied, a new release will be created.')


class Error(Exception):
    """Base class for exceptions in this module."""

class CaptureFailedError(Error):
    """Capturing a page screenshot failed."""


# URL regex rewriting code originally from mirrorrr
# http://code.google.com/p/mirrorrr/source/browse/trunk/transform_content.py

# URLs that have absolute addresses
ABSOLUTE_URL_REGEX = r"(?P<url>(http(s?):)?//[^\"'> \t]+)"
# URLs that are relative to the base of the current hostname.
BASE_RELATIVE_URL_REGEX = (
    r"/(?!(/)|(http(s?)://)|(url\())(?P<url>[^\"'> \t]*)")
# URLs that have '../' or './' to start off their paths.
TRAVERSAL_URL_REGEX = (
    r"(?P<relative>\.(\.)?)/(?!(/)|"
    r"(http(s?)://)|(url\())(?P<url>[^\"'> \t]*)")
# URLs that are in the same directory as the requested URL.
SAME_DIR_URL_REGEX = r"(?!(/)|(http(s?)://)|(#)|(url\())(?P<url>[^\"'> \t]+)"
# URL matches the root directory.
ROOT_DIR_URL_REGEX = r"(?!//(?!>))/(?P<url>)(?=[ \t\n]*[\"'> /])"
# Start of a tag using 'src' or 'href'
TAG_START = (
    r"(?i)(?P<tag>\ssrc|href|action|url|background)"
    r"(?P<equals>[\t ]*=[\t ]*)(?P<quote>[\"']?)")
# Potential HTML document URL with no fragments.
MAYBE_HTML_URL_REGEX = (
    TAG_START + r"(?P<absurl>(http(s?):)?//[^\"'> \t]+)")

REPLACEMENT_REGEXES = [
    (TAG_START + SAME_DIR_URL_REGEX,
     "\g<tag>\g<equals>\g<quote>%(accessed_dir)s\g<url>"),
    (TAG_START + TRAVERSAL_URL_REGEX,
     "\g<tag>\g<equals>\g<quote>%(accessed_dir)s/\g<relative>/\g<url>"),
    (TAG_START + BASE_RELATIVE_URL_REGEX,
     "\g<tag>\g<equals>\g<quote>%(base)s/\g<url>"),
    (TAG_START + ROOT_DIR_URL_REGEX,
     "\g<tag>\g<equals>\g<quote>%(base)s/"),
    (TAG_START + ABSOLUTE_URL_REGEX,
     "\g<tag>\g<equals>\g<quote>\g<url>"),
]


def clean_url(url, force_scheme=None):
    """Cleans the given URL."""
    # Collapse ../../ and related
    url_parts = urlparse.urlparse(url)
    path_parts = []
    for part in url_parts.path.split('/'):
        if part == '.':
            continue
        elif part == '..':
            if path_parts:
                path_parts.pop()
        else:
            path_parts.append(part)

    url_parts = list(url_parts)
    if force_scheme:
        url_parts[0] = force_scheme
    url_parts[2] = '/'.join(path_parts)
    url_parts[4] = ''    # No query string
    url_parts[5] = ''    # No path

    # Always have a trailing slash
    if not url_parts[2]:
        url_parts[2] = '/'

    return urlparse.urlunparse(url_parts)


def extract_urls(url, data, unescape=HTMLParser.HTMLParser().unescape):
    """Extracts the URLs from an HTML document."""
    parts = urlparse.urlparse(url)
    prefix = '%s://%s' % (parts.scheme, parts.netloc)

    accessed_dir = os.path.dirname(parts.path)
    if not accessed_dir.endswith('/'):
        accessed_dir += '/'

    for pattern, replacement in REPLACEMENT_REGEXES:
        fixed = replacement % {
            'base': prefix,
            'accessed_dir': accessed_dir,
        }
        data = re.sub(pattern, fixed, data)

    result = set()
    for match in re.finditer(MAYBE_HTML_URL_REGEX, data):
        found_url = unescape(match.groupdict()['absurl'])
        found_url = clean_url(
            found_url,
            force_scheme=parts[0])  # Use the main page's scheme
        result.add(found_url)

    return result


IGNORE_SUFFIXES = frozenset([
    'jpg', 'jpeg', 'png', 'css', 'js', 'xml', 'json', 'gif', 'ico', 'doc'])


def prune_urls(url_set, start_url, allowed_list, ignored_list):
    """Prunes URLs that should be ignored."""
    result = set()

    for url in url_set:
        allowed = False
        for allow_url in allowed_list:
            if url.startswith(allow_url):
                allowed = True
                break

        if not allowed:
            continue

        ignored = False
        for ignore_url in ignored_list:
            if url.startswith(ignore_url):
                ignored = True
                break

        if ignored:
            continue

        prefix, suffix = (url.rsplit('.', 1) + [''])[:2]
        if suffix.lower() in IGNORE_SUFFIXES:
            continue

        result.add(url)

    return result


class SiteDiff(workers.WorkflowItem):
    """Workflow for coordinating the site diff.

    Args:
        start_url: URL to begin the site diff scan.
        ignore_prefixes: Optional. List of URL prefixes to ignore during
            the crawl; start_url should be a common prefix with all of these.
        upload_build_id: Optional. Build ID of the site being compared. When
            supplied a new release will be cut for this build comparing it
            to the last good release.
        upload_release_name: Optional. Release name to use for the build. When
            not supplied, a new release based on the current time will be
            created.
        heartbeat: Function to call with progress status.
    """

    def run(self,
            start_url,
            ignore_prefixes,
            upload_build_id=None,
            upload_release_name=None,
            heartbeat=None):
        output_dir = tempfile.mkdtemp()

        if not ignore_prefixes:
            ignore_prefixes = []

        pending_urls = set([clean_url(start_url)])
        seen_urls = set()
        good_urls = set()

        yield heartbeat('Scanning for content')

        limit_depth = FLAGS.crawl_depth >= 0
        depth = 0
        while (not limit_depth or depth <= FLAGS.crawl_depth) and pending_urls:
            # TODO: Enforce a job-wide timeout on the whole process of
            # URL discovery, to make sure infinitely deep sites do not
            # cause this job to never stop.
            seen_urls.update(pending_urls)
            output = yield [workers.FetchItem(u) for u in pending_urls]
            pending_urls.clear()

            for item in output:
                if not item.data:
                    logging.debug('No data from url=%r', item.url)
                    continue

                if item.headers.gettype() != 'text/html':
                    logging.debug('Skipping non-HTML document url=%r',
                                  item.url)
                    continue

                good_urls.add(item.url)
                found = extract_urls(item.url, item.data)
                pruned = prune_urls(
                    found, start_url, [start_url], ignore_prefixes)
                new = pruned - seen_urls
                pending_urls.update(new)
                yield heartbeat('Found %d new URLs from %s' % (
                                len(new), item.url))

            yield heartbeat('Finished crawl at depth %d' % depth)
            depth += 1

        yield heartbeat(
            'Found %d total URLs, %d good HTML pages; starting '
            'screenshots' % (len(seen_urls), len(good_urls)))

        # TODO: Make the default release name prettier.
        if not upload_release_name:
            upload_release_name = str(datetime.datetime.utcnow())

        release_number = yield release_worker.CreateReleaseWorkflow(
            upload_build_id, upload_release_name, start_url)

        run_requests = []
        for url in good_urls:
            parts = urlparse.urlparse(url)
            run_name = parts.path

            # TODO: Include some more config options.
            config_data = json.dumps({
                'viewportSize': {
                    'width': 1024,
                    'height': 768,
                }
            })

            run_requests.append(release_worker.RequestRunWorkflow(
                upload_build_id, upload_release_name, release_number,
                run_name, url, config_data=config_data))

        yield run_requests

        yield heartbeat('Marking runs as complete')
        release_url = yield release_worker.RunsDoneWorkflow(
            upload_build_id, upload_release_name, release_number)

        yield heartbeat('Results viewable at: %s' % release_url)


class PrintWorkflow(workers.WorkflowItem):
    """Prints a message to stdout."""

    def run(self, message):
        yield []  # Make this into a generator
        print message


def real_main(start_url=None,
              ignore_prefixes=None,
              upload_build_id=None,
              upload_release_name=None,
              coordinator=None):
    """Runs the site_diff."""
    if not coordinator:
        coordinator = workers.get_coordinator()
    capture_worker.register(coordinator)
    pdiff_worker.register(coordinator)
    coordinator.start()

    item = SiteDiff(
        start_url=start_url,
        ignore_prefixes=ignore_prefixes,
        upload_build_id=upload_build_id,
        upload_release_name=upload_release_name,
        heartbeat=PrintWorkflow)
    item.root = True
    coordinator.input_queue.put(item)
    coordinator.wait_until_interrupted()


def main(argv):
    gflags.MarkFlagAsRequired('upload_build_id')
    gflags.MarkFlagAsRequired('release_server_prefix')

    try:
        argv = FLAGS(argv)
    except gflags.FlagsError, e:
        print '%s\nUsage: %s ARGS\n%s' % (e, sys.argv[0], FLAGS)
        sys.exit(1)

    if len(argv) != 2:
        print 'Must supply a website URL as the first argument.'
        sys.exit(1)

    if FLAGS.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    real_main(
        start_url=argv[1],
        ignore_prefixes=FLAGS.ignore_prefixes,
        upload_build_id=FLAGS.upload_build_id,
        upload_release_name=FLAGS.upload_release_name)


if __name__ == '__main__':
    main(sys.argv)
