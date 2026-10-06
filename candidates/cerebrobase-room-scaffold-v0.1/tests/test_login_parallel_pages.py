import re
import unittest
from support import ServerCase, jbody

class ParallelLoginPages(ServerCase):
    def test_both_identities_use_older_form_after_parallel_get(self):
        for identity, own, foreign in (
            ('fixture-andreas-admin','room-fixture-andreas-admin','room-fixture-marianne-pilot'),
            ('fixture-marianne-pilot','room-fixture-marianne-pilot','room-fixture-andreas-admin'),
        ):
            c = self.client()
            _, _, body = c.get('/logg-inn')
            first = re.search(rb'name="prelogin" value="([^"]+)"',body).group(1).decode()
            c.get('/')
            c.get('/logg-inn')
            status, headers, _ = c.post('/logg-inn',{'identity':identity,'prelogin':first})
            self.assertEqual((status,headers['Location']),(303,'/rom'))
            self.assertEqual(c.get('/rom')[1]['Location'],'/rom/'+own)
            self.assertEqual(c.get('/rom/'+own)[0],200)
            self.assertEqual(c.get('/rom/'+own)[0],200) # refresh
            self.assertEqual(c.get('/rom/'+foreign)[0],404)
            token = c.cookies['cb_session']
            self.assertEqual(c.post('/logg-ut',{'csrf':c.csrf()})[0],303)
            c.cookies['cb_session'] = token
            self.assertEqual(c.get('/rom/'+own)[0],303)

    def test_forged_missing_cookie_and_foreign_origin_still_rejected(self):
        c = self.client()
        c.get('/logg-inn')
        token = c.cookies['cb_prelogin']
        self.assertEqual(jbody(c.post('/logg-inn',{'identity':'fixture-andreas-admin','prelogin':'forged'})[2])['error'],'CSRF_REJECTED')
        self.assertEqual(jbody(c.post('/logg-inn',{'identity':'fixture-andreas-admin','prelogin':token},headers={'Origin':'https://evil.test'})[2])['error'],'ORIGIN_REJECTED')
        c.cookies.clear()
        self.assertEqual(jbody(c.post('/logg-inn',{'identity':'fixture-andreas-admin','prelogin':token})[2])['error'],'CSRF_REJECTED')

if __name__ == '__main__':
    unittest.main()
