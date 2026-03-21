"""Tests for the spectrum classification and labeling helpers in rtx_converter."""

import os
import tempfile
import unittest
import base64
import struct
from unittest.mock import MagicMock
from rtx_converter import (
    _sanitize_label,
    _classify_spectrum,
    _deduplicate_label,
    _build_spectrum_label,
    _atomic_number_to_symbol,
    _decode_binary_float64_series,
    LineScanExporter,
    EMSAExporter,
    MetadataExporter,
)

INDEX_HTML_PATH = os.path.join(os.path.dirname(__file__), 'index.html')


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

    def test_scan(self):
        self.assertEqual(_classify_spectrum('Scan', self._nonzero), 'line')

    def test_scan_numbered(self):
        self.assertEqual(_classify_spectrum('Scan 1', self._nonzero), 'line')

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

    def test_deconv_scan(self):
        self.assertEqual(_classify_spectrum('Scan Deconvoluted', self._nonzero), 'deconv_line')

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


class TestElementMapping(unittest.TestCase):
    def test_atomic_number_to_symbol(self):
        self.assertEqual(_atomic_number_to_symbol(26), 'fe')
        self.assertEqual(_atomic_number_to_symbol(14), 'si')
        self.assertEqual(_atomic_number_to_symbol(79), 'au')

    def test_atomic_number_out_of_range(self):
        self.assertIsNone(_atomic_number_to_symbol(0))
        self.assertIsNone(_atomic_number_to_symbol(999))


class TestLineScanDecoding(unittest.TestCase):
    def test_decode_binary_float64_series(self):
        vals = [1.0, 2.5, 7.25]
        payload = struct.pack('<ddd', *vals)
        encoded = base64.b64encode(payload).decode('ascii')
        got = _decode_binary_float64_series(encoded, expected_size=len(payload))
        self.assertEqual(got, vals)


class TestLabelIntegration(unittest.TestCase):
    """Verify that _classify_spectrum + _sanitize_label produce the expected
    descriptive labels used in filenames."""

    _nonzero = [0, 1, 2, 3]

    def _build_label(self, name, counts, index=0):
        """Replicate the label-building logic from convert_rtx_file."""
        return _build_spectrum_label(name, counts, index)

    def test_spot_name_preserves_index(self):
        # "Spot 12" → spot_012 with zero-padded index
        self.assertEqual(self._build_label('Spot 12', self._nonzero), 'spot_012')

    def test_sum_uses_canonical(self):
        self.assertEqual(self._build_label('Sum', self._nonzero), 'sum')

    def test_line_uses_line_label(self):
        # "Line 2" → line_002 with zero-padded index
        self.assertEqual(self._build_label('Line 2', self._nonzero), 'line_002')

    def test_background_uses_canonical(self):
        self.assertEqual(self._build_label('Background', self._nonzero), 'background')

    def test_calibration_named_standard(self):
        # "Standard" is classified as calibration → canonical "calibration"
        self.assertEqual(self._build_label('Standard', self._nonzero), 'calibration')

    def test_deconv_spot_preserves_distinction(self):
        # Deconvoluted spot spectra keep the _spot suffix
        self.assertEqual(
            self._build_label('Spot 1 Deconvoluted', self._nonzero),
            'deconv_spot_001',
        )

    def test_empty_name_uses_type(self):
        # Empty name gets 'spectrum' type (index is handled by deduplication)
        self.assertEqual(self._build_label('', self._nonzero, index=2), 'spectrum')

    def test_unknown_name_uses_type(self):
        # "Unknown" is classified as generic 'spectrum'
        self.assertEqual(self._build_label('Unknown', self._nonzero, index=0), 'spectrum')

    def test_empty_counts_preserves_type_and_index(self):
        # Empty spot with index → empty_spot_001 (zero-padded)
        self.assertEqual(self._build_label('Spot 1', [], index=0), 'empty_spot_001')

    def test_generic_name_uses_spectrum(self):
        # Generic names (not matching any category) use 'spectrum'
        self.assertEqual(self._build_label('Sample X', self._nonzero), 'spectrum')

    def test_scan_uses_line_label(self):
        # "Scan" is classified as 'line' type
        self.assertEqual(self._build_label('Scan', self._nonzero), 'line')

    def test_deconv_line_preserves_distinction(self):
        # Deconvoluted line spectra keep the _line suffix
        self.assertEqual(self._build_label('Line 1 Fitted', self._nonzero), 'deconv_line_001')

    def test_deconv_line_uses_element_symbol_when_available(self):
        self.assertEqual(
            _build_spectrum_label(
                'deconv',
                self._nonzero,
                index=0,
                index_map={},
                dominant_type=None,
                deconv_element_symbol='fe',
            ),
            'deconv_line_fe',
        )

    def test_empty_deconv_line_uses_element_symbol_when_available(self):
        self.assertEqual(
            _build_spectrum_label(
                'deconv',
                [0, 0, 0],
                index=0,
                index_map={},
                dominant_type=None,
                deconv_element_symbol='si',
            ),
            'empty_deconv_line_si',
        )

    def test_deconv_spot_uses_element_symbol_when_available(self):
        self.assertEqual(
            _build_spectrum_label(
                'Spot 3 Deconvoluted',
                self._nonzero,
                index=0,
                index_map={},
                dominant_type=None,
                deconv_element_symbol='ca',
            ),
            'deconv_spot_ca',
        )

    def test_empty_deconv_spot_uses_element_symbol_when_available(self):
        self.assertEqual(
            _build_spectrum_label(
                'Spot 3 Deconvoluted',
                [0, 0, 0],
                index=0,
                index_map={},
                dominant_type=None,
                deconv_element_symbol='ca',
            ),
            'empty_deconv_spot_ca_003',
        )

    def test_empty_deconv_label(self):
        self.assertEqual(self._build_label('Deconv', [0, 0, 0]), 'empty_deconv')

    def test_empty_line_label(self):
        # "Scan" is classified as 'line' → empty_line
        self.assertEqual(self._build_label('Scan', [0, 0, 0]), 'empty_line')

    def test_empty_generic_label(self):
        # Empty with no name → empty_spectrum
        self.assertEqual(self._build_label('', [0, 0, 0], index=4), 'empty_spectrum')


class TestLabelDeduplication(unittest.TestCase):
    def test_multi_instance_type_sequential_numbering(self):
        """Multi-instance types (spot, line, etc.) get sequential 001, 002..."""
        seen = set()
        type_counts = {}
        labels = [
            _deduplicate_label('spot', seen, type_counts),
            _deduplicate_label('spot', seen, type_counts),
            _deduplicate_label('spot', seen, type_counts),
        ]
        self.assertEqual(labels, ['spot_001', 'spot_002', 'spot_003'])

    def test_single_instance_type_dup_suffix(self):
        """Single-instance types (background, etc.) get _dup2, _dup3..."""
        seen = set()
        type_counts = {}
        labels = [
            _deduplicate_label('background', seen, type_counts),
            _deduplicate_label('background', seen, type_counts),
            _deduplicate_label('background', seen, type_counts),
        ]
        self.assertEqual(labels, ['background', 'background_dup2', 'background_dup3'])

    def test_preserved_index_dup_suffix(self):
        """Labels with preserved indices get _dup suffix for true duplicates."""
        seen = set()
        type_counts = {}
        labels = [
            _deduplicate_label('spot_012', seen, type_counts),
            _deduplicate_label('spot_012', seen, type_counts),
            _deduplicate_label('spot_005', seen, type_counts),
        ]
        self.assertEqual(labels, ['spot_012', 'spot_012_dup2', 'spot_005'])

    def test_deconv_element_label_dup_suffix(self):
        """Element-based deconv labels deduplicate deterministically."""
        seen = set()
        type_counts = {}
        labels = [
            _deduplicate_label('deconv_spot_fe', seen, type_counts),
            _deduplicate_label('deconv_spot_fe', seen, type_counts),
            _deduplicate_label('deconv_line_fe', seen, type_counts),
            _deduplicate_label('deconv_line_fe', seen, type_counts),
        ]
        self.assertEqual(
            labels,
            ['deconv_spot_fe', 'deconv_spot_fe_dup2', 'deconv_line_fe', 'deconv_line_fe_dup2'],
        )


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
        with open(INDEX_HTML_PATH, encoding='utf-8') as f:
            content = f.read()

        self.assertIn('.download-link {', content)
        block = content.split('.download-link {', 1)[1].split('}', 1)[0]
        self.assertIn('overflow-wrap: break-word;', block)
        self.assertIn('word-break: break-all;', block)


class TestLineScanExporter(unittest.TestCase):
    def test_export_block_csv_and_element_msa(self):
        parser = _FakeParser([{'name': 'Spot 1', 'counts': [1, 2, 3], 'meta': {}}])
        exporter = LineScanExporter(parser)
        block = {
            'result_type': 'ROISum',
            'scan_length': 10.0,
            'series': [
                {'name': 'Fe', 'values': [10.0, 20.0, 30.0], 'n_points': 3},
                {'name': 'Si', 'values': [5.0, 7.0, 9.0], 'n_points': 3},
            ],
        }

        with tempfile.NamedTemporaryFile(mode='r', suffix='.csv', delete=False) as fcsv:
            csv_path = fcsv.name
        with tempfile.NamedTemporaryFile(mode='r', suffix='.msa', delete=False) as fmsa:
            msa_path = fmsa.name
        try:
            exporter.export_block_csv(block, csv_path)
            exporter.export_element_msa(
                block,
                block['series'][0],
                msa_path,
                title='line_scan_block_01_roisum_fe_intensity_vs_position',
            )
            with open(csv_path, encoding='utf-8') as f:
                csv_text = f.read()
            with open(msa_path, encoding='latin-1') as f:
                msa_text = f.read()

            self.assertIn('PointIndex,PositionFraction_0to1,PositionScanUnits,Intensity_fe,Intensity_si', csv_text)
            self.assertIn('#XUNITS      : point_index', msa_text)
            self.assertIn('#YUNITS      : intensity', msa_text)
            self.assertIn('#SIGNALTYPE  : EDS_LINE_PROFILE', msa_text)
        finally:
            os.unlink(csv_path)
            os.unlink(msa_path)


if __name__ == '__main__':
    unittest.main()
