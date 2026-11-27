import unittest
from unittest.mock import Mock
from collector.transfer import buffered_response,QuietProgress,BUFFER_BYTES


class TransferTests(unittest.TestCase):
    def test_larger_buffer_preserves_original_bytes(self):
        response=Mock()
        payload=[b'original',b' apk bytes']
        response.iter_content=Mock(return_value=iter(payload))
        original=response.iter_content
        buffered_response(response,RuntimeError,Mock())
        self.assertEqual(b''.join(response.iter_content(chunk_size=8192)),b'original apk bytes')
        original.assert_called_once_with(chunk_size=BUFFER_BYTES,decode_unicode=False)

    def test_midstream_error_is_reported_as_retryable(self):
        def broken(**kwargs):
            yield b'partial'
            raise RuntimeError('connection failed')
        response=Mock(iter_content=broken)
        report=Mock()
        buffered_response(response,RuntimeError,report)
        with self.assertRaises(RuntimeError):
            list(response.iter_content(8192))
        report.assert_called_once_with('transient','The transfer connection was interrupted.')

    def test_progress_is_discarded_and_error_context_is_bounded(self):
        stream=QuietProgress()
        for i in range(1000):
            stream.write('\r  Progress: '+str(i))
        self.assertEqual(stream.details(),'')
        for i in range(200):
            stream.write('error context '+str(i))
        self.assertEqual(len(stream.messages),128)
        self.assertIn('error context 199',stream.details())
