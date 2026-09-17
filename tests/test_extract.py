"""Archive extraction preserves executables and rejects unsafe archive entries."""
import io
import tarfile
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch
from test_deploy import m


class ExtractTests(unittest.TestCase):
    def archive(self, root, name='bin/tool', kind=tarfile.REGTYPE):
        archive=root/'package.tar'
        with tarfile.open(archive,'w') as tf:
            entry=tarfile.TarInfo(name);entry.type=kind;entry.mode=0o755
            if kind==tarfile.REGTYPE:
                entry.size=4;tf.addfile(entry,io.BytesIO(b'test'))
            else:
                entry.linkname='../outside';tf.addfile(entry)
        return archive

    def test_extract_without_deprecation_warning_preserves_executable(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);archive=self.archive(root)
            with warnings.catch_warnings():
                warnings.simplefilter('error',DeprecationWarning)
                m.extract(archive,root/'out')
            target=root/'out/bin/tool'
            self.assertEqual(target.read_bytes(),b'test')
            self.assertEqual(target.stat().st_mode & 0o111,0o111)

    def test_explicit_data_filter(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);archive=self.archive(root)
            with patch.object(tarfile.TarFile,'extractall',autospec=True) as extractall:
                m.extract(archive,root/'out')
            self.assertEqual(extractall.call_args.kwargs,{'filter':'data'})

    def test_unsafe_members_rejected_before_extraction(self):
        for name,kind in [('../outside',tarfile.REGTYPE),('/outside',tarfile.REGTYPE),
                          ('link',tarfile.SYMTYPE),('hardlink',tarfile.LNKTYPE),('device',tarfile.CHRTYPE)]:
            with self.subTest(name=name),tempfile.TemporaryDirectory() as td:
                root=Path(td);archive=self.archive(root,name,kind)
                with patch.object(tarfile.TarFile,'extractall') as extractall:
                    with self.assertRaises(RuntimeError):m.extract(archive,root/'out')
                    extractall.assert_not_called()

    def test_older_python_without_filter_uses_validated_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);archive=self.archive(root)
            with patch.object(tarfile.TarFile,'extractall',autospec=True) as extractall:
                with patch.object(m,'hasattr',side_effect=lambda obj,name: False if obj is tarfile and name=='data_filter' else hasattr(obj,name),create=True):
                    m.extract(archive,root/'out')
            self.assertEqual(extractall.call_args.kwargs,{})
