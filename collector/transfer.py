"""Reduce streaming overhead without parallel requests or identity rotation."""
BUFFER_BYTES=256*1024


def buffered_response(response,request_exception,on_error):
    original=response.iter_content
    def iterate(chunk_size=1,decode_unicode=False):
        try:
            yield from original(chunk_size=max(chunk_size or 0,BUFFER_BYTES),decode_unicode=decode_unicode)
        except request_exception:
            on_error('transient','The transfer connection was interrupted.')
            raise
    response.iter_content=iterate
    return response


class QuietProgress:
    """Drop repetitive progress output, retain bounded failure context in memory."""
    def __init__(self):
        from collections import deque
        self.messages=deque(maxlen=128)

    def write(self,text):
        if not text.startswith('\r  Progress:'):
            self.messages.append(text[:2048])
        return len(text)

    def flush(self):
        pass

    def details(self):
        return ''.join(self.messages)
