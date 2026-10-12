"""Private bearer/client-secret requests never follow a redirect to another origin."""
from urllib.request import HTTPRedirectHandler,build_opener

class NoPrivateRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        raise RuntimeError('PRIVATE_HTTP_REDIRECT_REFUSED')

def private_open(request,*,timeout):
    return build_opener(NoPrivateRedirect()).open(request,timeout=timeout)
