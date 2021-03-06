import logging
import os
import re
import shutil
import tempfile
import time
import unicodedata
import urllib2
import api
import youtube
from datetime import datetime, timedelta
from os.path import splitext
from collections import defaultdict
from progressbar import ProgressBar
from boto.s3.connection import S3Connection, OrdinaryCallingFormat
from boto.s3.key import Key
from boto.exception import BotoServerError
from secrets import (
    s3_access_key, 
    s3_secret_key, 
    archive_access_key, 
    archive_secret_key
)
from util import logger, DOWNLOADABLE_FORMATS

# We use bucket names with uppercase characters, so we must use OrdinaryCallingFormat
# instead of the default SubdomainCallingFormat
s3_connection = S3Connection(s3_access_key, s3_secret_key, calling_format=OrdinaryCallingFormat())

converted_bucket = s3_connection.get_bucket("KA-youtube-converted")
unconverted_bucket = s3_connection.get_bucket("KA-youtube-unconverted")

archive_connection = S3Connection(archive_access_key, archive_secret_key, host="s3.us.archive.org", is_secure=False, calling_format=OrdinaryCallingFormat())
# You can up the num_retries for the connection like this:
#archive_connection.num_retries = 12
# However, S3Connection uses exponential backoff which is a bit excessive.
# Instead we have a loop below for archive.org uploads, to retry more aggressively

# Keys (inside buckets) are in the format YOUTUBE_ID.FORMAT
# e.g. DK1lCc9b7bg.mp4/ or Dpo_-GrMpNE.m3u8/
re_video_key_name = re.compile(r"([\w-]+)\.(\w+)/")

# Older keys are of the form YOUTUBE_ID
re_legacy_video_key_name = re.compile(r"([\w-]+)/(.*)$")

def get_or_create_unconverted_source_url(youtube_id):
    matching_keys = list(unconverted_bucket.list(youtube_id))
    matching_key = None

    if len(matching_keys) > 0:
        if len(matching_keys) > 1:
            logger.warning("More than 1 matching unconverted video URL found for video {0}".format(youtube_id))
        matching_key = matching_keys[0]
    else:
        logger.info("Unconverted video not available on s3 yet, downloading from youtube to create it.")

        video_path = youtube.download(youtube_id)
        logger.info("Downloaded video to {0}".format(video_path))

        assert(video_path)

        video_extension = splitext(video_path)[1]
        assert video_extension[0] == "."
        video_extension = video_extension[1:]
        if video_extension not in ["flv", "mp4"]:
            logger.warning("Unrecognized video extension {0} when downloading video {1} from YouTube".format(video_extension, youtube_id))

        matching_key = Key(unconverted_bucket, "{0}/{0}.{1}".format(youtube_id, video_extension))
        matching_key.set_contents_from_filename(video_path)

        os.remove(video_path)
        logger.info("Deleted {0}".format(video_path))

    return "s3://{0}/{1}".format(unconverted_bucket.name, matching_key.name)

def list_converted_formats():
    """Returns a dict that maps youtube_ids (keys) to a set of available converted formats (values)"""
    converted_videos = defaultdict(set)
    legacy_video_keys = set()
    for key in converted_bucket.list(delimiter="/"):
        video_match = re_video_key_name.match(key.name)
        if video_match is None:
            if re_legacy_video_key_name.match(key.name) is not None:
                legacy_video_keys.add(key.name)
            else:
                logger.warning("Unrecognized key {0} is not in format YOUTUBE_ID.FORMAT/".format(key.name))
        else:
            converted_videos[video_match.group(1)].add(video_match.group(2))
    logger.info("{0} legacy converted videos were ignored".format(len(legacy_video_keys)))
    return converted_videos

def list_legacy_mp4_videos():
    """Returns a set with youtube ids of videos that have legacy mp4/png converted content saved on S3. You can pass these ids to copy_legacy_content_to_new_location."""
    legacy_mp4_videos = set()
    for key in converted_bucket.list(delimiter="/"):
        legacy_match = re_legacy_video_key_name.match(key.name)
        if legacy_match is not None:
            legacy_mp4_videos.add(legacy_match.group(1))
    return legacy_mp4_videos

def copy_legacy_content_to_new_location(youtube_id):
    """Copies the MP4 & PNG files from a legacy-format video in the S3 converted bucket to the new naming scheme."""
    for key in converted_bucket.list(prefix="{0}/".format(youtube_id)):
        legacy_match = re_legacy_video_key_name.match(key.name)
        assert legacy_match is not None
        assert legacy_match.group(1) == youtube_id
        dest_key = "{0}.mp4/{1}".format(youtube_id, legacy_match.group(2))
        logger.info("Copying {0} to {1}".format(key.name, dest_key))
        key.copy(converted_bucket.name, dest_key, preserve_acl=True)

def list_missing_converted_formats():
    """Returns a dict that maps youtube_ids (keys) to a set of formats missing from the converted bucket (values)"""
    missing_converted_formats = {}
    converted_formats = list_converted_formats()
    for playlist in api.get_library():
        for video in playlist["videos"]:
            if "youtube_id" not in video: continue # Non-YouTube video
            missing_converted_formats[video["youtube_id"]] = DOWNLOADABLE_FORMATS - converted_formats[video["youtube_id"]]
    return missing_converted_formats

def upload_converted_to_archive(youtube_id, formats_to_upload):
    # The bucket may not exist yet on archive.org. Unfortunately create_bucket
    # is broken in boto (it requires all-lowercase, despite the fact that
    # we're using OrdinaryCallingFormat). Fortunately get_bucket can be told
    # to not check that the bucket exists with the validate=False flag, and 
    # the "x-archive-auto-make-bucket" header we pass below automatically
    # creates the bucket with the first upload.
    dest_bucket = archive_connection.get_bucket("KA-converted-{0}".format(youtube_id), validate=False)

    source_keys_for_format = defaultdict(list)
    for key in list(converted_bucket.list(youtube_id)):
        video_match = re_video_key_name.match(key.name)
        if video_match is None:
            if re_legacy_video_key_name.match(key.name) is None:
                logger.info("Unrecognized file in converted bucket {0}".format(key.name))
            continue

        assert video_match.group(1) == youtube_id
        format = video_match.group(2)

        modification_date = datetime.strptime(key.last_modified, "%Y-%m-%dT%H:%M:%S.%fZ")
        if datetime.utcnow() - modification_date < timedelta(hours=1):
            logger.error("Format {0} for video {1} appeared ready on S3, but further inspection showed Zencoder may still be uploading it. Modification date {2}".format(format, youtube_id, modification_date))
            return False

        # Maps format (mp4, m3u8, etc) to list of keys
        source_keys_for_format[format].append(key)

    for format in formats_to_upload:
        if len(source_keys_for_format[format]) == 0:
            logger.error("Requested upload of format {0} for video {1} to archive.org, but unable to find video format in converted bucket".format(format, youtube_id))
            return False

    # Fetch the video metadata so we can specify title and description to archive.org
    video_metadata = api.video_metadata(youtube_id)

    # Only pass ascii title and descriptions in headers to archive, and strip newlines
    def normalize_for_header(s):
        return unicodedata.normalize("NFKD", s or u"").encode("ascii", "ignore").replace("\n", "")

    uploaded_filenames = []

    for format in formats_to_upload:
        for key in source_keys_for_format[format]:
            video_match = re_video_key_name.match(key.name)
            assert video_match.group(1) == youtube_id
            assert video_match.group(2) == format
            video_prefix = video_match.group()
            assert key.name.startswith(video_prefix)
            destination_name = key.name[len(video_prefix):]
            assert "/" not in destination_name # Don't expect more than one level of nesting
            
            logger.debug("Copying file {0} to archive.org".format(destination_name))
            
            with tempfile.TemporaryFile() as t:
                pbar = ProgressBar(maxval=100).start()
                def get_cb(bytes_sent, bytes_total):
                    pbar.update(50.0 * bytes_sent / bytes_total)
                def send_cb(bytes_sent, bytes_total):
                    pbar.update(50.0 + 50.0 * bytes_sent / bytes_total)
                key.get_file(t, cb=get_cb)
                
                t.seek(0)
                dest_key = Key(dest_bucket, destination_name)
                headers = {
                    "x-archive-auto-make-bucket": "1",
                    "x-archive-meta-collection": "khanacademy", 
                    "x-archive-meta-title": normalize_for_header(video_metadata["title"]),
                    "x-archive-meta-description": normalize_for_header(video_metadata["description"]),
                    "x-archive-meta-mediatype": "movies", 
                    "x-archive-meta01-subject": "Salman Khan", 
                    "x-archive-meta02-subject": "Khan Academy",
                }
                for attempt in xrange(10):
                    try:
                        dest_key.set_contents_from_file(t, headers=headers, cb=send_cb)
                        break
                    except BotoServerError as e:
                        logger.error("Error {0} {1} during upload attempt {2} to archive.org.".format(e.status, e.reason, attempt))
                else:
                    raise Exception("Gave up publish attempt due to server errors")
                pbar.finish()
                
                uploaded_filenames.append(destination_name)

    logger.debug("Waiting 10 seconds for uploads to propagate")
    time.sleep(10)

    for destination_name in uploaded_filenames:
        if verify_archive_upload(youtube_id, destination_name):
            logger.error("Verified upload {0}/{1}".format(youtube_id, destination_name))
        else:
            logger.error("Unable to verify upload {0}/{1}".format(youtube_id, destination_name))
            return False

    return True

def verify_archive_upload(youtube_id, filename):
    c_retries_allowed = 5
    c_retries = 0

    while c_retries < c_retries_allowed:
        try:
            request = urllib2.Request("http://s3.us.archive.org/KA-converted-{0}/{1}".format(youtube_id, filename))

            request.get_method = lambda: "HEAD"
            response = urllib2.urlopen(request)

            return response.code == 200
        except urllib2.HTTPError, e:
            c_retries += 1

            if c_retries < c_retries_allowed:
                logger.error("Error during archive upload verification attempt %s, trying again" % c_retries)
            else:
                logger.error("Error during archive upload verification final attempt: %s" % e)

            time.sleep(10)

    return False
