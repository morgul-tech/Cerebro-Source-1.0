import os,tempfile,unittest
from pathlib import Path
from support import config_dict
from cerebrobase.config import validate_config,ConfigError
class HardlinkBoundary(unittest.TestCase):
 def test_cross_directory_same_db_inode_denied(self):
  with tempfile.TemporaryDirectory() as d:
   b=Path(d);prod=b/'prod/db/rooms.sqlite3';prod.parent.mkdir(parents=True);prod.write_bytes(b'isolated-fixture')
   stage=b/'stage/data/db/rooms.sqlite3';stage.parent.mkdir(parents=True);os.link(prod,stage)
   self.assertTrue(os.path.samefile(stage,prod))
   cfg=config_dict(b/'stage',environment='STAGING',build_id='0123456789abcdef',production_roots={'db_path':str(prod),'private_data_root':str(b/'prod/private'),'runtime_root':str(b/'prod/run')})
   with self.assertRaises(ConfigError) as exc:validate_config(cfg)
   self.assertIn('STAGING_POINTS_INTO_PRODUCTION',{p['code'] for p in exc.exception.problems})
 def test_distinct_files_with_identical_bytes_allowed(self):
  with tempfile.TemporaryDirectory() as d:
   b=Path(d);prod=b/'prod/db/rooms.sqlite3';prod.parent.mkdir(parents=True);prod.write_bytes(b'fixture')
   stage=b/'stage/data/db/rooms.sqlite3';stage.parent.mkdir(parents=True);stage.write_bytes(b'fixture')
   self.assertFalse(os.path.samefile(stage,prod))
   cfg=config_dict(b/'stage',environment='STAGING',build_id='0123456789abcdef',production_roots={'db_path':str(prod),'private_data_root':str(b/'prod/private'),'runtime_root':str(b/'prod/run')})
   self.assertEqual(validate_config(cfg).environment,'STAGING')
