"""Tests for the spectrum classification and labeling helpers in rtx_converter."""

import os
import tempfile
import unittest
from unittest.mock import MagicMock
from rtx_converter import _sanitize_label, _classify_spectrum, EMSAExporter, MetadataExporter


class TestSanitizeLabel(unittest.TestCase):
    def test_basic_spot(self):
        self.assertEqual(_sanitize_label('Spot 12'), 'spot_12')

    def test_sum(self):
        self.assertEqual(_sanitize_label('Sum'), 'sum')

    def test_special_chars(self):
        self.assertEqual(_sanitize_label('Line-Scan #3'), 'line_scan_3')

    def test_empty(self):
        self.assertEqual(_sanitize_label(''), '')

    def test_whitespace_only(self):
        self.assertEqual(_sanitize_label('   '), '')


class TestClassifySpectrum(unittest.TestCase):
    """Verify _classify_spectrum returns the correct category."""

    _nonzero = [0, 1, 2, 3]  # representative non-empty spectrum

    # -- empty / metadata-only --
    def test_empty_counts(self):
        self.assertEqual(_classify_spectrum('Spot 1', []), 'empty')

    def test_all_zero_counts(self):
        self.assertEqual(_classify_spectrum('Spot 1', [0, 0, 0]), 'empty')

    # -- spot --
    def test_spot(self):
        self.assertEqual(_classify_spectrum('Spot 1', self._nonzero), 'spot')

    def test_spot_caps(self):
        self.assertEqual(_classify_spectrum('SPOT 5', self._nonzero), 'spot')

    def test_point(self):
        self.assertEqual(_classify_spectrum('Point 3', self._nonzero), 'spot')

    # -- line --
    def test_line(self):
        self.assertEqual(_classify_spectrum('Line 2', self._nonzero), 'line')

    def test_linescan(self):
        self.assertEqual(_classify_spectrum('Linescan 1', self._nonzero), 'line')

    def test_profile(self):
        self.assertEqual(_classify_spectrum('Profile 4', self._nonzero), 'line')

    # -- deconvoluted --
    def test_deconv(self):
        self.assertEqual(_classify_spectrum('Deconvoluted', self._nonzero), 'deconv')

    def test_deconv_short(self):
        self.assertEqual(_classify_spectrum('Deconv', self._nonzero), 'deconv')

    def test_fitted(self):
        self.assertEqual(_classify_spectrum('Fitted', self._nonzero), 'deconv')

    def test_deconv_spot(self):
        self.assertEqual(_classify_spectrum('Spot 1 Deconvoluted', self._nonzero), 'deconv_spot')

    def test_deconv_line(self):
        self.assertEqual(_classify_spectrum('Linescan 2 Deconvoluted', self._nonzero), 'deconv_line')

    def test_deconv_point(self):
        self.assertEqual(_classify_spectrum('Point 3 Fit', self._nonzero), 'deconv_spot')

    # -- background --
    def test_background(self):
        self.assertEqual(_classify_spectrum('Background', self._nonzero), 'background')

    def test_bremsstrahlung(self):
        self.assertEqual(_classify_spectrum('Bremsstrahlung', self._nonzero), 'background')

    def test_bg(self):
        self.assertEqual(_classify_spectrum('BG', self._nonzero), 'background')

    # -- calibration --
    def test_calibration(self):
        self.assertEqual(_classify_spectrum('Calibration', self._nonzero), 'calibration')

    def test_calib(self):
        self.assertEqual(_classify_spectrum('Calib', self._nonzero), 'calibration')

    def test_standard(self):
        self.assertEqual(_classify_spectrum('Standard', self._nonzero), 'calibration')

    # -- sum --
    def test_sum(self):
        self.assertEqual(_classify_spectrum('Sum', self._nonzero), 'sum')

    def test_total(self):
        self.assertEqual(_classify_spectrum('Total', self._nonzero), 'sum')

    def test_mean(self):
        self.assertEqual(_classify_spectrum('Mean', self._nonzero), 'sum')

    def test_average(self):
        self.assertEqual(_classify_spectrum('Average', self._nonzero), 'sum')

    # -- generic --
    def test_generic_name(self):
        self.assertEqual(_classify_spectrum('MySpectrum', self._nonzero), 'spectrum')

    def test_unknown(self):
        self.assertEqual(_classify_spectrum('Unknown', self._nonzero), 'spectrum')

    def test_numeric_only(self):
        self.assertEqual(_classify_spectrum('42', self._nonzero), 'spectrum')


class TestLabelIntegration(unittest.TestCase):
    """Verify that _classify_spectrum + _sanitize_label produce the expected
    descriptive labels used in filenames."""

    _nonzero = [0, 1, 2, 3]

    def _build_label(self, name, counts, index=0):
        """Replicate the label-building logic from convert_rtx_file."""
        raw = _sanitize_label(name)
        spec_type = _classify_spectrum(name, counts)

        if not raw or raw == 'unknown':
            raw = f'{spec_type}_{index + 1}'
        else:
            type_root = spec_type.split('_')[0]
            if spec_type != 'spectrum' and not raw.startswith(type_root):
                raw = f'{spec_type}_{raw}'
        return raw

    def test_spot_name_keeps_label(self):
        # "Spot 12" already starts with "spot" so no prefix added
        self.assertEqual(self._build_label('Spot 12', self._nonzero), 'spot_12')

    def test_sum_prepends_type(self):
        self.assertEqual(self._build_label('Sum', self._nonzero), 'sum')

    def test_line_name_keeps_label(self):
        self.assertEqual(self._build_label('Line 2', self._nonzero), 'line_2')

    def test_background_prepends(self):
        # "Background" already starts with "background"
        self.assertEqual(self._build_label('Background', self._nonzero), 'background')

    def test_calibration_named_standard(self):
        # "Standard" does not start with "calib" so type is prepended
        self.assertEqual(self._build_label('Standard', self._nonzero), 'calibration_standard')

    def test_deconv_spot_compound(self):
        self.assertEqual(
            self._build_label('Spot 1 Deconvoluted', self._nonzero),
            'deconv_spot_spot_1_deconvoluted',
        )

    def test_empty_name_uses_type(self):
        self.assertEqual(self._build_label('', self._nonzero, index=2), 'spectrum_3')

    def test_unknown_name_uses_type(self):
        self.assertEqual(self._build_label('Unknown', self._nonzero, index=0), 'spectrum_1')

    def test_empty_counts_empty_type(self):
        self.assertEqual(self._build_label('Spot 1', [], index=0), 'empty_spot_1')

    def test_generic_name_no_prefix(self):
        # Generic type "spectrum" is never prepended
        self.assertEqual(self._build_label('Sample X', self._nonzero), 'sample_x')


class _FakeParser:
    """Minimal stand-in for RTXParser used by exporter tests."""

    def __init__(self, spectra):
        self.filepath = '/tmp/fake.rtx'
        self.spectra = spectra
        self.metadata = {}
        self.sem_metadata = {}

    def energy_calibration(self, idx=0):
        return (0.0, 10.0)

    def beam_kV(self):
        return 0.0

    def live_time_s(self, idx=0):
        return 0.0

    def real_time_s(self, idx=0):
        return 0.0

    def _format_date_emsa(self):
        return ''

    def _format_time_emsa(self):
        return ''


class TestEMSATitleUsesLabel(unittest.TestCase):
    """Verify that the EMSA exporter writes the informative label into #TITLE."""

    def _export_and_read(self, spec_name, title=None):
        parser = _FakeParser([{'name': spec_name, 'counts': [1, 2, 3], 'meta': {}}])
        with tempfile.NamedTemporaryFile(mode='r', suffix='.msa', delete=False) as f:
            path = f.name
        try:
            EMSAExporter(parser).export(path, 0, title=title)
            with open(path, encoding='latin-1') as f:
                return f.read()
        finally:
            os.unlink(path)

    def test_title_uses_label_when_provided(self):
        content = self._export_and_read('Spot 12', title='spot_12')
        self.assertIn('#TITLE       : spot_12', content)

    def test_title_falls_back_to_spec_name(self):
        content = self._export_and_read('Spot 12')
        self.assertIn('#TITLE       : Spot 12', content)

    def test_title_falls_back_to_stem(self):
        content = self._export_and_read('')
        self.assertIn('#TITLE       : fake', content)

    def test_deconv_label_in_title(self):
        content = self._export_and_read('Result', title='deconv_result')
        self.assertIn('#TITLE       : deconv_result', content)


class TestMetadataUsesLabels(unittest.TestCase):
    """Verify that the metadata exporter uses informative labels."""

    def _export_and_read(self, spectra, labels=None):
        parser = _FakeParser(spectra)
        with tempfile.NamedTemporaryFile(mode='r', suffix='.txt', delete=False) as f:
            path = f.name
        try:
            MetadataExporter(parser).export(path, labels=labels)
            with open(path) as f:
                return f.read()
        finally:
            os.unlink(path)

    def test_spectrum_summary_uses_labels(self):
        spectra = [
            {'name': 'Spot 12', 'counts': [1, 2, 3], 'meta': {}},
            {'name': 'Sum', 'counts': [4, 5, 6], 'meta': {}},
        ]
        content = self._export_and_read(spectra, labels=['spot_12', 'sum'])
        self.assertIn('Spectrum 1: spot_12', content)
        self.assertIn('Spectrum 2: sum', content)

    def test_spectrum_summary_falls_back_to_raw_name(self):
        spectra = [{'name': 'Spot 12', 'counts': [1, 2, 3], 'meta': {}}]
        content = self._export_and_read(spectra)
        self.assertIn('Spectrum 1: Spot 12', content)

    def test_timing_section_uses_labels(self):
        spectra = [
            {'name': 'Spot 12', 'counts': [1, 2, 3], 'meta': {}},
            {'name': 'Background', 'counts': [4, 5, 6], 'meta': {}},
        ]
        content = self._export_and_read(spectra, labels=['spot_12', 'background'])
        self.assertIn('[spot_12]', content)
        self.assertIn('[background]', content)


class TestWebAppStyles(unittest.TestCase):
    """Verify the download links can display long spectrum filenames."""

    def test_download_links_wrap_long_names(self):
        index_path = os.path.join(os.path.dirname(__file__), 'index.html')
        with open(index_path, encoding='utf-8') as f:
            content = f.read()

        self.assertIn('.download-link {', content)
        self.assertIn('overflow-wrap: break-word;', content)
        self.assertIn('word-break: break-all;', content)


if __name__ == '__main__':
    unittest.main()
