# Copyright 2017 Google Inc.
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

import base64
import copy
import hashlib
import io
import os

import google.auth
import google.auth.transport.requests as tr_requests
import pytest
from google.resumable_media import common
from six.moves import http_client

from google import resumable_media
import google.resumable_media.requests as resumable_requests
import google.resumable_media.requests.download as download_mod
from tests.system import utils


CURR_DIR = os.path.dirname(os.path.realpath(__file__))
DATA_DIR = os.path.join(CURR_DIR, u'..', u'..', u'data')
PLAIN_TEXT = u'text/plain'
IMAGE_JPEG = u'image/jpeg'
ALL_FILES = (
    {
        u'path': os.path.realpath(os.path.join(DATA_DIR, u'image1.jpg')),
        u'content_type': IMAGE_JPEG,
        u'checksum': u'1bsd83IYNug8hd+V1ING3Q==',
        u'slices': (
            slice(1024, 16386, None),  # obj[1024:16386]
            slice(None, 8192, None),  # obj[:8192]
            slice(-256, None, None),  # obj[-256:]
            slice(262144, None, None),  # obj[262144:]
        ),
    }, {
        u'path': os.path.realpath(os.path.join(DATA_DIR, u'image2.jpg')),
        u'content_type': IMAGE_JPEG,
        u'checksum': u'gdLXJltiYAMP9WZZFEQI1Q==',
        u'slices': (
            slice(1024, 16386, None),  # obj[1024:16386]
            slice(None, 8192, None),  # obj[:8192]
            slice(-256, None, None),  # obj[-256:]
            slice(262144, None, None),  # obj[262144:]
        ),
    }, {
        u'path': os.path.realpath(os.path.join(DATA_DIR, u'file.txt')),
        u'content_type': PLAIN_TEXT,
        u'checksum': u'KHRs/+ZSrc/FuuR4qz/PZQ==',
        u'slices': (),
    }, {
        u'path': os.path.realpath(os.path.join(DATA_DIR, u'gzipped.txt.gz')),
        u'uncompressed':
            os.path.realpath(os.path.join(DATA_DIR, u'gzipped.txt')),
        u'content_type': PLAIN_TEXT,
        u'checksum': u'KHRs/+ZSrc/FuuR4qz/PZQ==',
        u'slices': (),
        u'metadata': {
            u'contentEncoding': u'gzip',
        },
    },
)
ENCRYPTED_ERR = (
    b'The target object is encrypted by a customer-supplied encryption key.')
NO_BODY_ERR = (
    u'The content for this response was already consumed')
NOT_FOUND_ERR = (
    b'No such object: ' +
    bytes(os.environ['GOOGLE_RESUMABLE_MEDIA_BUCKET'], 'utf-8') +
    b'/does-not-exist.txt'
)


class CorruptingAuthorizedSession(tr_requests.AuthorizedSession):
    """A Requests Session class with credentials, which corrupts responses.

    This class is used for testing checksum validation.

    Args:
        credentials (google.auth.credentials.Credentials): The credentials to
            add to the request.
        refresh_status_codes (Sequence[int]): Which HTTP status codes indicate
            that credentials should be refreshed and the request should be
            retried.
        max_refresh_attempts (int): The maximum number of times to attempt to
            refresh the credentials and retry the request.
        kwargs: Additional arguments passed to the :class:`requests.Session`
            constructor.
    """

    EMPTY_HASH = base64.b64encode(
        hashlib.md5(b'').digest()).decode(u'utf-8')

    def request(self, method, url, data=None, headers=None, **kwargs):
        """Implementation of Requests' request."""
        response = tr_requests.AuthorizedSession.request(
            self, method, url, data=data, headers=headers, **kwargs)
        response.headers[download_mod._HASH_HEADER] = (
            u'md5={}'.format(self.EMPTY_HASH))
        return response


@pytest.fixture(scope=u'module')
def authorized_transport():
    credentials, _ = google.auth.default(scopes=(utils.GCS_RW_SCOPE,))
    yield tr_requests.AuthorizedSession(credentials)


# Transport that returns corrupt data, so we can exercise checksum handling.
@pytest.fixture(scope=u'module')
def corrupting_transport():
    credentials, _ = google.auth.default(scopes=(utils.GCS_RW_SCOPE,))
    yield CorruptingAuthorizedSession(credentials)


def delete_blob(transport, blob_name):
    metadata_url = utils.METADATA_URL_TEMPLATE.format(blob_name=blob_name)
    response = transport.delete(metadata_url)
    assert response.status_code == http_client.NO_CONTENT


def _get_contents_for_upload(info):
    with open(info[u'path'], u'rb') as file_obj:
        return file_obj.read()


def _get_contents(info):
    full_path = info.get(u'uncompressed', info[u'path'])
    with open(full_path, u'rb') as file_obj:
        return file_obj.read()


def _get_blob_name(info):
    full_path = info.get(u'uncompressed', info[u'path'])
    return os.path.basename(full_path)


@pytest.fixture(scope=u'module')
def add_files(authorized_transport):
    blob_names = []
    for info in ALL_FILES:
        to_upload = _get_contents_for_upload(info)
        blob_name = _get_blob_name(info)

        blob_names.append(blob_name)
        if u'metadata' in info:
            upload = resumable_requests.MultipartUpload(utils.MULTIPART_UPLOAD)
            metadata = copy.deepcopy(info[u'metadata'])
            metadata[u'name'] = blob_name
            response = upload.transmit(
                authorized_transport, to_upload,
                metadata, info[u'content_type'])
        else:
            upload_url = utils.SIMPLE_UPLOAD_TEMPLATE.format(
                blob_name=blob_name)
            upload = resumable_requests.SimpleUpload(upload_url)
            response = upload.transmit(
                authorized_transport, to_upload, info[u'content_type'])

        assert response.status_code == http_client.OK

    yield

    # Clean-up the blobs we created.
    for blob_name in blob_names:
        delete_blob(authorized_transport, blob_name)


def check_tombstoned(download, transport):
    assert download.finished
    if isinstance(download, resumable_requests.Download):
        with pytest.raises(ValueError) as exc_info:
            download.consume(transport)
        assert exc_info.match(u'A download can only be used once.')
    else:
        with pytest.raises(ValueError) as exc_info:
            download.consume_next_chunk(transport)
        assert exc_info.match(u'Download has finished.')


def test_download_full(add_files, authorized_transport):
    for info in ALL_FILES:
        actual_contents = _get_contents(info)
        blob_name = _get_blob_name(info)

        # Create the actual download object.
        media_url = utils.DOWNLOAD_URL_TEMPLATE.format(blob_name=blob_name)
        download = resumable_requests.Download(media_url)
        # Consume the resource.
        response = download.consume(authorized_transport)
        assert response.status_code == http_client.OK
        assert response.content == actual_contents
        check_tombstoned(download, authorized_transport)


def test_download_to_stream(add_files, authorized_transport):
    for info in ALL_FILES:
        actual_contents = _get_contents(info)
        blob_name = _get_blob_name(info)

        # Create the actual download object.
        media_url = utils.DOWNLOAD_URL_TEMPLATE.format(blob_name=blob_name)
        stream = io.BytesIO()
        download = resumable_requests.Download(media_url, stream=stream)
        # Consume the resource.
        response = download.consume(authorized_transport)
        assert response.status_code == http_client.OK
        with pytest.raises(RuntimeError) as exc_info:
            getattr(response, u'content')
        assert exc_info.value.args == (NO_BODY_ERR,)
        assert response._content is False
        assert response._content_consumed is True
        assert stream.getvalue() == actual_contents
        check_tombstoned(download, authorized_transport)


def test_corrupt_download(add_files, corrupting_transport):
    for info in ALL_FILES:
        blob_name = _get_blob_name(info)

        # Create the actual download object.
        media_url = utils.DOWNLOAD_URL_TEMPLATE.format(blob_name=blob_name)
        stream = io.BytesIO()
        download = resumable_requests.Download(media_url, stream=stream)
        # Consume the resource.
        with pytest.raises(common.DataCorruption) as exc_info:
            download.consume(corrupting_transport)

        assert download.finished
        msg = download_mod._CHECKSUM_MISMATCH.format(
            download.media_url, CorruptingAuthorizedSession.EMPTY_HASH,
            info[u'checksum'])
        assert exc_info.value.args == (msg,)


@pytest.fixture(scope=u'module')
def secret_file(authorized_transport):
    blob_name = u'super-seekrit.txt'
    data = b'Please do not tell anyone my encrypted seekrit.'

    upload_url = utils.SIMPLE_UPLOAD_TEMPLATE.format(blob_name=blob_name)
    headers = utils.get_encryption_headers()
    upload = resumable_requests.SimpleUpload(upload_url, headers=headers)
    response = upload.transmit(authorized_transport, data, PLAIN_TEXT)
    assert response.status_code == http_client.OK

    yield blob_name, data, headers

    delete_blob(authorized_transport, blob_name)


def check_error_response(exc_info, status_code, message):
    error = exc_info.value
    response = error.response
    assert response.status_code == status_code
    assert response.content.startswith(message)
    assert len(error.args) == 5
    assert error.args[1] == status_code
    assert error.args[3] == http_client.OK
    assert error.args[4] == http_client.PARTIAL_CONTENT


def test_extra_headers(authorized_transport, secret_file):
    blob_name, data, headers = secret_file
    # Create the actual download object.
    media_url = utils.DOWNLOAD_URL_TEMPLATE.format(blob_name=blob_name)
    download = resumable_requests.Download(media_url, headers=headers)
    # Consume the resource.
    response = download.consume(authorized_transport)
    assert response.status_code == http_client.OK
    assert response.content == data
    check_tombstoned(download, authorized_transport)
    # Attempt to consume the resource **without** the headers.
    download_wo = resumable_requests.Download(media_url)
    with pytest.raises(resumable_media.InvalidResponse) as exc_info:
        download_wo.consume(authorized_transport)

    check_error_response(exc_info, http_client.BAD_REQUEST, ENCRYPTED_ERR)
    check_tombstoned(download_wo, authorized_transport)


def test_non_existent_file(authorized_transport):
    blob_name = u'does-not-exist.txt'
    media_url = utils.DOWNLOAD_URL_TEMPLATE.format(blob_name=blob_name)
    download = resumable_requests.Download(media_url)

    # Try to consume the resource and fail.
    with pytest.raises(resumable_media.InvalidResponse) as exc_info:
        download.consume(authorized_transport)
    check_error_response(exc_info, http_client.NOT_FOUND, NOT_FOUND_ERR)
    check_tombstoned(download, authorized_transport)


@pytest.fixture(scope=u'module')
def simple_file(authorized_transport):
    blob_name = u'basic-file.txt'
    upload_url = utils.SIMPLE_UPLOAD_TEMPLATE.format(blob_name=blob_name)
    upload = resumable_requests.SimpleUpload(upload_url)
    data = b'Simple contents'
    response = upload.transmit(authorized_transport, data, PLAIN_TEXT)
    assert response.status_code == http_client.OK

    yield blob_name, data

    delete_blob(authorized_transport, blob_name)


def test_bad_range(simple_file, authorized_transport):
    blob_name, data = simple_file
    # Make sure we have an invalid range.
    start = 32
    end = 63
    assert len(data) < start < end
    # Create the actual download object.
    media_url = utils.DOWNLOAD_URL_TEMPLATE.format(blob_name=blob_name)
    download = resumable_requests.Download(media_url, start=start, end=end)

    # Try to consume the resource and fail.
    with pytest.raises(resumable_media.InvalidResponse) as exc_info:
        download.consume(authorized_transport)

    check_error_response(
        exc_info, http_client.REQUESTED_RANGE_NOT_SATISFIABLE,
        b'Request range not satisfiable')
    check_tombstoned(download, authorized_transport)


def _download_slice(media_url, slice_):
    assert slice_.step is None

    end = None
    if slice_.stop is not None:
        end = slice_.stop - 1

    return resumable_requests.Download(
        media_url, start=slice_.start, end=end)


def test_download_partial(add_files, authorized_transport):
    for info in ALL_FILES:
        actual_contents = _get_contents(info)
        blob_name = _get_blob_name(info)

        media_url = utils.DOWNLOAD_URL_TEMPLATE.format(blob_name=blob_name)
        for slice_ in info[u'slices']:
            download = _download_slice(media_url, slice_)
            response = download.consume(authorized_transport)
            assert response.status_code == http_client.PARTIAL_CONTENT
            assert response.content == actual_contents[slice_]
            with pytest.raises(ValueError):
                download.consume(authorized_transport)


def get_chunk_size(min_chunks, total_bytes):
    # Make sure the number of chunks **DOES NOT** evenly divide.
    num_chunks = min_chunks
    while total_bytes % num_chunks == 0:
        num_chunks += 1

    chunk_size = total_bytes // num_chunks
    # Since we know an integer division has remainder, increment by 1.
    chunk_size += 1
    assert total_bytes < num_chunks * chunk_size

    return num_chunks, chunk_size


def consume_chunks(download, authorized_transport,
                   total_bytes, actual_contents):
    start_byte = download.start
    end_byte = download.end
    if end_byte is None:
        end_byte = total_bytes - 1

    num_responses = 0
    while not download.finished:
        response = download.consume_next_chunk(authorized_transport)
        num_responses += 1

        next_byte = min(start_byte + download.chunk_size, end_byte + 1)
        assert download.bytes_downloaded == next_byte - download.start
        assert download.total_bytes == total_bytes
        assert response.status_code == http_client.PARTIAL_CONTENT
        assert response.content == actual_contents[start_byte:next_byte]
        start_byte = next_byte

    return num_responses, response


def test_chunked_download(add_files, authorized_transport):
    for info in ALL_FILES:
        actual_contents = _get_contents(info)
        blob_name = _get_blob_name(info)

        total_bytes = len(actual_contents)
        num_chunks, chunk_size = get_chunk_size(7, total_bytes)
        # Create the actual download object.
        media_url = utils.DOWNLOAD_URL_TEMPLATE.format(blob_name=blob_name)
        stream = io.BytesIO()
        download = resumable_requests.ChunkedDownload(
            media_url, chunk_size, stream)
        # Consume the resource in chunks.
        num_responses, last_response = consume_chunks(
            download, authorized_transport,
            total_bytes, actual_contents)
        # Make sure the combined chunks are the whole object.
        assert stream.getvalue() == actual_contents
        # Check that we have the right number of responses.
        assert num_responses == num_chunks
        # Make sure the last chunk isn't the same size.
        assert total_bytes % chunk_size != 0
        assert len(last_response.content) < chunk_size
        check_tombstoned(download, authorized_transport)


def test_chunked_download_partial(add_files, authorized_transport):
    for info in ALL_FILES:
        actual_contents = _get_contents(info)
        blob_name = _get_blob_name(info)

        media_url = utils.DOWNLOAD_URL_TEMPLATE.format(blob_name=blob_name)
        for slice_ in info[u'slices']:
            # Manually replace a missing start with 0.
            start = 0 if slice_.start is None else slice_.start
            # Chunked downloads don't support a negative index.
            if start < 0:
                continue

            # First determine how much content is in the slice and
            # use it to determine a chunking strategy.
            total_bytes = len(actual_contents)
            if slice_.stop is None:
                end_byte = total_bytes - 1
                end = None
            else:
                # Python slices DO NOT include the last index, though a byte
                # range **is** inclusive of both endpoints.
                end_byte = slice_.stop - 1
                end = end_byte

            num_chunks, chunk_size = get_chunk_size(
                7, end_byte - start + 1)
            # Create the actual download object.
            stream = io.BytesIO()
            download = resumable_requests.ChunkedDownload(
                media_url, chunk_size, stream, start=start, end=end)
            # Consume the resource in chunks.
            num_responses, last_response = consume_chunks(
                download, authorized_transport, total_bytes, actual_contents)

            # Make sure the combined chunks are the whole slice.
            assert stream.getvalue() == actual_contents[slice_]
            # Check that we have the right number of responses.
            assert num_responses == num_chunks
            # Make sure the last chunk isn't the same size.
            assert len(last_response.content) < chunk_size
            check_tombstoned(download, authorized_transport)


def test_chunked_with_extra_headers(authorized_transport, secret_file):
    blob_name, data, headers = secret_file
    num_chunks = 4
    chunk_size = 12
    assert (num_chunks - 1) * chunk_size < len(data) < num_chunks * chunk_size
    # Create the actual download object.
    media_url = utils.DOWNLOAD_URL_TEMPLATE.format(blob_name=blob_name)
    stream = io.BytesIO()
    download = resumable_requests.ChunkedDownload(
        media_url, chunk_size, stream, headers=headers)
    # Consume the resource in chunks.
    num_responses, last_response = consume_chunks(
        download, authorized_transport, len(data), data)
    # Make sure the combined chunks are the whole object.
    assert stream.getvalue() == data
    # Check that we have the right number of responses.
    assert num_responses == num_chunks
    # Make sure the last chunk isn't the same size.
    assert len(last_response.content) < chunk_size
    check_tombstoned(download, authorized_transport)
    # Attempt to consume the resource **without** the headers.
    stream_wo = io.BytesIO()
    download_wo = resumable_requests.ChunkedDownload(
        media_url, chunk_size, stream_wo)
    with pytest.raises(resumable_media.InvalidResponse) as exc_info:
        download_wo.consume_next_chunk(authorized_transport)

    assert stream_wo.tell() == 0
    check_error_response(exc_info, http_client.BAD_REQUEST, ENCRYPTED_ERR)
    assert download_wo.invalid
