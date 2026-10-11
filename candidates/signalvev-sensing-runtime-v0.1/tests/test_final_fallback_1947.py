"""The ONE supplied X5 exact1947 changed-risk regression. No old suites."""
import hashlib
from context_slice_support import TmpRoot,request,ORIGINAL
from signalvev_sensing.context_slice import ResultEnvelopeUnavailable
class Exact1947(TmpRoot):
    def test_exact1947_rejected_before_original_content_open(self):
        req=request(selector={"start":0,"end":1024,"expected_slice_sha256":hashlib.sha256(ORIGINAL[:1024]).hexdigest()},limits={"max_result_bytes":1947})
        with self.assertRaises(ResultEnvelopeUnavailable):self.resolver().resolve(req)
        self.assertEqual((self.provider.stats.content_opens,self.provider.stats.bytes_read),(0,0))
        self.assertEqual(self.provider.stats.describes,1)
