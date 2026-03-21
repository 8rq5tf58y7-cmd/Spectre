#!/usr/bin/env python3
"""
Bruker RTX to Open Format Converter

Converts proprietary Bruker EDS .rtx files to open source formats:
- EMSA/MSA 1.0 format for NIST DTSA II
- CSV with energy-calibrated channels
- Metadata text report with instrument and run information

RTX internals:
  Outer XML  -> <TRTProject> with zlib-compressed, base64-encoded <RTData>
  Inner XML  -> <CompData> tree of ClassInstance nodes:
    TRTSpectrum           : EDS sum spectrum (Channels = comma-separated counts)
    TRTSpectrumHeader     : CalibAbs/CalibLin (keV), ChannelCount, Date/Time
    TRTSpectrumHardwareHeader : RealTime/LifeTime (ms), DeadTime (%), ShapingTime
    TRTESMAHeader         : PrimaryEnergy (keV), ElevationAngle, AzimutAngle
    TRTDetectorHeader     : detector info
    TRTREMHeader          : Energy (kV), Magnification, WorkingDistance (mm)
    TRTImageData          : map image planes (Data elements, base64-encoded)
"""

import xml.etree.ElementTree as ET
import zlib
import base64
import os
import sys
import struct
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple
import argparse
import re


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Periodic table symbols indexed by atomic number (1-based).
_PERIODIC_TABLE_SYMBOLS: List[str] = [
    '', 'h', 'he', 'li', 'be', 'b', 'c', 'n', 'o', 'f', 'ne',
    'na', 'mg', 'al', 'si', 'p', 's', 'cl', 'ar', 'k', 'ca',
    'sc', 'ti', 'v', 'cr', 'mn', 'fe', 'co', 'ni', 'cu', 'zn',
    'ga', 'ge', 'as', 'se', 'br', 'kr', 'rb', 'sr', 'y', 'zr',
    'nb', 'mo', 'tc', 'ru', 'rh', 'pd', 'ag', 'cd', 'in', 'sn',
    'sb', 'te', 'i', 'xe', 'cs', 'ba', 'la', 'ce', 'pr', 'nd',
    'pm', 'sm', 'eu', 'gd', 'tb', 'dy', 'ho', 'er', 'tm', 'yb',
    'lu', 'hf', 'ta', 'w', 're', 'os', 'ir', 'pt', 'au', 'hg',
    'tl', 'pb', 'bi', 'po', 'at', 'rn', 'fr', 'ra', 'ac', 'th',
    'pa', 'u', 'np', 'pu', 'am', 'cm', 'bk', 'cf', 'es', 'fm',
    'md', 'no', 'lr', 'rf', 'db', 'sg', 'bh', 'hs', 'mt', 'ds',
    'rg', 'cn', 'nh', 'fl', 'mc', 'lv', 'ts', 'og',
]


def _atomic_number_to_symbol(atomic_number: int) -> Optional[str]:
    """Return lowercase element symbol for an atomic number, if valid."""
    if 1 <= atomic_number < len(_PERIODIC_TABLE_SYMBOLS):
        return _PERIODIC_TABLE_SYMBOLS[atomic_number]
    return None

def _sanitize_label(name: str) -> str:
    """Turn a spectrum name (e.g. 'Spot 12') into a filesystem-safe label
    (e.g. 'spot_12').  Collapses whitespace/special chars into underscores,
    strips leading/trailing underscores, and lower-cases the result."""
    label = re.sub(r'[^\w]+', '_', name)   # non-alphanumeric -> '_'
    label = label.strip('_').lower()
    return label


def _classify_spectrum(name: str, counts: List[int]) -> str:
    """Classify a spectrum into a descriptive category based on its name and
    channel data.

    Returns one of:
      'spot'        – point / spot acquisition
      'line'        – line-scan or profile acquisition
      'deconv_spot' – deconvoluted spot spectrum
      'deconv_line' – deconvoluted line spectrum
      'deconv'      – deconvoluted (type unspecified)
      'background'  – background / bremsstrahlung
      'calibration' – calibration or standard spectrum
      'sum'         – sum / total / average / integrated spectrum
      'empty'       – no counts or all-zero channels (metadata-only)
      'spectrum'    – generic / unclassified
    """
    lower = name.lower().strip()

    # Empty / metadata-only (counts list is empty or all zeros)
    if not counts or all(c == 0 for c in counts):
        return 'empty'

    # Deconvoluted / fitted – check before spot/line so compound names
    # like "Spot 1 Deconvoluted" are correctly classified.
    if re.search(r'\bdeconv(?:olut(?:ed|ion)?)?\b|\bfit(?:ted)?\b', lower):
        if re.search(r'\bspot\b|\bpoint\b|\bspectrum\b', lower):
            return 'deconv_spot'
        if re.search(r'\bline(?:scan)?\b|\bprofile\b|\bscan\b', lower):
            return 'deconv_line'
        return 'deconv'

    # Background / bremsstrahlung
    if re.search(r'\bbackground\b|\bbremsstrahlung\b|\bbg\b', lower):
        return 'background'

    # Calibration / standard
    if re.search(r'\bcalib(?:ration)?\b|\bstandard\b', lower):
        return 'calibration'

    # Spot / point spectrum – includes "Spectrum X" which in EDS context
    # typically refers to a point/spot acquisition (numbered spectra)
    if re.search(r'\bspot\b|\bpoint\b', lower):
        return 'spot'
    
    # "Spectrum X" pattern (numbered spectrum) is typically a spot acquisition
    if re.search(r'\bspectrum\s*\d+\b', lower):
        return 'spot'

    # Line-scan / profile / scan spectrum
    if re.search(r'\bline(?:scan)?\b|\bprofile\b|\bscan\b', lower):
        return 'line'

    # Sum / total / average / integrated
    if re.search(r'\bsum\b|\btotal\b|\bintegral\b|\bmean\b|\baverage\b', lower):
        return 'sum'

    return 'spectrum'


def _deduplicate_label(label: str, seen: Set[str], type_counts: Dict[str, int] = None) -> str:
    """Return a unique label, handling duplicates appropriately.
    
    - Labels with preserved indices (e.g., spot_012) are kept as-is if unique
    - True duplicates get a _dup2, _dup3 suffix
    - Labels without indices get sequential numbering (001, 002...)
    """
    if type_counts is None:
        type_counts = {}
    
    # Check if label already has a numeric index (from original spectrum name)
    has_index = bool(re.search(r'_\d{3}$', label))
    
    if has_index:
        # Label already has an index (e.g., spot_012) - use as-is if unique
        if label not in seen:
            seen.add(label)
            return label
        # True duplicate - add _dup suffix
        dup_num = 2
        while True:
            candidate = f'{label}_dup{dup_num}'
            if candidate not in seen:
                seen.add(candidate)
                return candidate
            dup_num += 1
    else:
        # No index - assign sequential numbering
        # Extract base type for counting
        base_label = label
        
        # Multi-instance types get sequential numbers
        multi_instance_types = {'spot', 'line', 'deconv', 'deconv_spot', 'deconv_line', 'spectrum'}
        
        if base_label in multi_instance_types:
            type_counts[base_label] = type_counts.get(base_label, 0) + 1
            count = type_counts[base_label]
            candidate = f'{base_label}_{count:03d}'
            # Ensure uniqueness
            while candidate in seen:
                count += 1
                type_counts[base_label] = count
                candidate = f'{base_label}_{count:03d}'
            seen.add(candidate)
            return candidate
        else:
            # Single-instance types (background, calibration, sum)
            if label not in seen:
                seen.add(label)
                return label
            # Duplicate - add _dup suffix
            dup_num = 2
            while True:
                candidate = f'{label}_dup{dup_num}'
                if candidate not in seen:
                    seen.add(candidate)
                    return candidate
                dup_num += 1


# Canonical filename label for each spectrum type.
# Each type has a distinct, self-describing prefix for clear identification.
_TYPE_CANONICAL: Dict[str, Optional[str]] = {
    'spectrum':    'spectrum',
    'spot':        'spot',
    'line':        'line',
    'deconv':      'deconv',
    'deconv_spot': 'deconv_spot',
    'deconv_line': 'deconv_line',
    'background':  'background',
    'calibration': 'calibration',
    'sum':         'sum',
    'empty':       'empty',
}


def _extract_numeric_index(name: str) -> Optional[int]:
    """Extract numeric index from spectrum name like 'Spot 12' or 'Line Scan 3'.
    
    Returns the extracted integer or None if no number found.
    """
    match = re.search(r'\b(\d+)\b', name)
    if match:
        return int(match.group(1))
    return None


def _build_index_type_map(spectra: List[Dict]) -> Dict[int, str]:
    """Build a map of numeric indices to their spectrum types (spot/line).
    
    This is used to infer the type of deconvoluted spectra when the deconv
    spectrum name doesn't explicitly contain 'spot' or 'line', but its index
    matches a known spot or line spectrum.
    
    Example: If we have 'Spot 5' and 'Deconvoluted 5', we can infer that
    'Deconvoluted 5' is a deconv_spot.
    """
    index_map: Dict[int, str] = {}
    
    for sp in spectra:
        name = sp.get('name', '')
        counts = sp.get('counts', [])
        
        # Skip empty spectra
        if not counts or all(c == 0 for c in counts):
            continue
            
        spec_type = _classify_spectrum(name, counts)
        
        # Only map spot and line types
        if spec_type in ('spot', 'line'):
            idx = _extract_numeric_index(name)
            if idx is not None and idx not in index_map:
                index_map[idx] = spec_type
    
    return index_map


def _get_dominant_spectrum_type(spectra: List[Dict]) -> Optional[str]:
    """Determine if all acquisition spectra are of the same type (spot or line).
    
    Returns 'spot' or 'line' if all non-deconv, non-background, non-calibration,
    non-sum spectra are of that type. Returns None if there's a mix or no spectra.
    
    This is used to infer deconv type when individual correlation isn't possible.
    """
    spot_count = 0
    line_count = 0
    
    for sp in spectra:
        name = sp.get('name', '')
        counts = sp.get('counts', [])
        
        if not counts or all(c == 0 for c in counts):
            continue
            
        spec_type = _classify_spectrum(name, counts)
        
        if spec_type == 'spot':
            spot_count += 1
        elif spec_type == 'line':
            line_count += 1
    
    # If we have only spots (no lines), return spot
    if spot_count > 0 and line_count == 0:
        return 'spot'
    # If we have only lines (no spots), return line  
    if line_count > 0 and spot_count == 0:
        return 'line'
    
    return None


def _classify_spectrum_with_context(name: str, counts: List[int], 
                                     index_map: Dict[int, str] = None,
                                     dominant_type: str = None,
                                     deconv_element_symbol: Optional[str] = None) -> str:
    """Classify a spectrum, using context to infer deconv type when needed.
    
    This extends _classify_spectrum by using context from other spectra in the
    file to determine if a generic 'deconv' should be 'deconv_spot' or 'deconv_line'.
    
    Args:
        name: Spectrum name
        counts: Channel counts
        index_map: Map of numeric indices to spectrum types (spot/line)
        dominant_type: The dominant spectrum type in the file ('spot' or 'line')
                      Used when index correlation isn't possible.
    """
    base_type = _classify_spectrum(name, counts)
    
    # If it's a generic deconv, try to infer spot/line
    if base_type == 'deconv':
        # If the RTX deconvolution metadata gives an element for this spectrum,
        # this corresponds to line deconvolution output in Bruker Esprit.
        if deconv_element_symbol:
            return 'deconv_line'

        # First try: correlate by index
        if index_map:
            idx = _extract_numeric_index(name)
            if idx is not None and idx in index_map:
                parent_type = index_map[idx]
                if parent_type == 'spot':
                    return 'deconv_spot'
                elif parent_type == 'line':
                    return 'deconv_line'
        
        # Second try: use dominant type if all spectra are same type
        if dominant_type == 'spot':
            return 'deconv_spot'
        elif dominant_type == 'line':
            return 'deconv_line'
    
    return base_type


def _build_spectrum_label(name: str, counts: List[int], index: int = 0,
                          index_map: Dict[int, str] = None,
                          dominant_type: str = None,
                          deconv_element_symbol: Optional[str] = None) -> str:
    """Build a short, self-describing label for a spectrum.

    Uses *canonical* type labels so filenames clearly distinguish spot
    from line spectra, deconvoluted variants, backgrounds, etc.
    Preserves numeric indices from original names (e.g., 'Spot 12' → 'spot_012').
    
    If index_map is provided, uses it to infer deconv_spot/deconv_line for
    generic deconvoluted spectra by matching indices with spot/line spectra.
    If dominant_type is provided, uses it as fallback for deconv spectra.

    Returns the label **before** deduplication.
    """
    raw = _sanitize_label(name)
    spec_type = _classify_spectrum_with_context(
        name, counts, index_map, dominant_type, deconv_element_symbol
    )
    
    # Extract numeric index from original name if present
    num_idx = _extract_numeric_index(name)

    if spec_type == 'empty':
        # Re-classify ignoring empty counts to determine the sub-type.
        inner_type = _classify_spectrum_with_context(
            name, [1], index_map, dominant_type, deconv_element_symbol
        )
        inner_canonical = _TYPE_CANONICAL.get(inner_type)
        if inner_canonical is not None:
            if inner_type in ('deconv_line', 'deconv_spot') and deconv_element_symbol:
                base = f'empty_{inner_type}_{deconv_element_symbol}'
            else:
                base = f'empty_{inner_canonical}'
        elif raw and raw != 'unknown':
            base = f'empty_{raw}'
        else:
            base = 'empty'
        # Append numeric index if present
        if num_idx is not None:
            return f'{base}_{num_idx:03d}'
        return base

    canonical = _TYPE_CANONICAL.get(spec_type)
    
    # For types that typically have numbered instances, append the index
    indexed_types = {'spot', 'line', 'deconv_spot', 'deconv_line', 'spectrum'}
    
    if canonical is not None:
        if spec_type in ('deconv_line', 'deconv_spot') and deconv_element_symbol:
            return f'{spec_type}_{deconv_element_symbol}'
        if canonical in indexed_types and num_idx is not None:
            return f'{canonical}_{num_idx:03d}'
        return canonical
    
    if raw and raw != 'unknown':
        return raw
    return f'spectrum_{index + 1:03d}'


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class RTXParser:
    """Parse a Bruker .rtx file and expose spectra + metadata."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.inner_root = None
        self.spectra: List[Dict] = []
        self.metadata: Dict[str, str] = {}
        self.sem_metadata: Dict[str, str] = {}
        self.deconv_element_symbols_by_spectrum_idx: Dict[int, str] = {}

    def parse(self) -> bool:
        try:
            tree = ET.parse(self.filepath)
            outer_root = tree.getroot()

            self._parse_outer_header(outer_root)

            rt_data = outer_root.find('.//RTData')
            if rt_data is None or not rt_data.text:
                raise ValueError("No RTData element found")

            b64 = rt_data.text.strip().replace('\n', '').replace('\r', '').replace(' ', '')
            raw = zlib.decompress(base64.b64decode(b64))
            self.inner_root = ET.fromstring(raw.decode('utf-8', errors='replace'))

            self._extract_global_metadata(self.inner_root)
            self._extract_spectra(self.inner_root)
            self._extract_deconv_element_context(self.inner_root)

            try:
                self._load_companion_sem_txt()
            except Exception as exc:
                print(f"  WARNING  : companion SEM .txt scan failed – {exc}")

            return True
        except Exception as exc:
            print(f"Error parsing {self.filepath}: {exc}")
            import traceback; traceback.print_exc()
            return False

    # -- outer header -------------------------------------------------------

    def _parse_outer_header(self, outer_root):
        ph = outer_root.find('.//RTHeader/ProjectHeader')
        if ph is not None:
            for tag in ('Date', 'Time', 'Creator', 'Comment'):
                el = ph.find(tag)
                if el is not None and el.text:
                    self.metadata[f'project_{tag.lower()}'] = el.text.strip()

    # -- global metadata from inner XML ------------------------------------

    _TAG_MAP = {
        'Date': 'acquisition_date',
        'Time': 'acquisition_time',
        'Energy': 'beam_energy_kV',
        'PrimaryEnergy': 'primary_energy_kV',
        'ElevationAngle': 'elevation_angle_deg',
        'AzimutAngle': 'azimuth_angle_deg',
        'Magnification': 'magnification',
        'WorkingDistance': 'working_distance_mm',
        'RealTime': 'real_time_ms',
        'LifeTime': 'live_time_ms',
        'DeadTime': 'dead_time_percent',
        'ChannelCount': 'channel_count',
        'CalibAbs': 'calib_abs_keV',
        'CalibLin': 'calib_lin_keV_per_ch',
        'SigmaAbs': 'sigma_abs',
        'SigmaLin': 'sigma_lin',
        'ZeroPeakPosition': 'zero_peak_position',
        'ZeroPeakFrequency': 'zero_peak_frequency',
        'PulseDensity': 'pulse_density',
        'Amplification': 'amplification',
        'ShapingTime': 'shaping_time_ns',
        'DetectorCount': 'detector_count',
        'SPVType': 'spv_type',
        'SPVRevision': 'spv_revision',
        'XCalibration': 'x_calibration_um_per_px',
        'YCalibration': 'y_calibration_um_per_px',
        'Width': 'image_width_px',
        'Height': 'image_height_px',
    }

    def _extract_global_metadata(self, root):
        for el in root.iter():
            if el.tag in self._TAG_MAP and el.text and el.text.strip():
                key = self._TAG_MAP[el.tag]
                if key not in self.metadata:
                    self.metadata[key] = el.text.strip()

        for ci in root.iter('ClassInstance'):
            if ci.get('Type') == 'TRTMapDataContainer':
                for tag, mkey in (('Date', 'map_date'), ('Time', 'map_time')):
                    el = ci.find(tag)
                    if el is not None and el.text:
                        self.metadata[mkey] = el.text.strip()

    # -- spectra extraction -------------------------------------------------

    def _extract_spectra(self, root):
        for ci in root.iter('ClassInstance'):
            if ci.get('Type') == 'TRTSpectrum':
                spec = self._parse_trt_spectrum(ci)
                if spec:
                    self.spectra.append(spec)

    def _extract_deconv_element_context(self, root):
        """Map deconvoluted TRTSpectrum entries to element symbols.

        Bruker stores deconvolution element IDs in TRTDeconvolutionResult blocks.
        We align those IDs with deconvoluted spectra in emission order.
        """
        symbols: List[str] = []
        for ci in root.iter('ClassInstance'):
            if ci.get('Type') != 'TRTDeconvolutionResult':
                continue

            el = ci.find('Element')
            if el is None or not el.text:
                continue
            try:
                atomic_number = int(el.text.strip())
            except ValueError:
                continue

            symbol = _atomic_number_to_symbol(atomic_number)
            if symbol:
                symbols.append(symbol)

        if not symbols:
            return

        deconv_indices: List[int] = []
        for i, sp in enumerate(self.spectra):
            name = sp.get('name', '')
            if re.search(r'\bdeconv(?:olut(?:ed|ion)?)?\b|\bfit(?:ted)?\b', name, re.IGNORECASE):
                deconv_indices.append(i)

        for spec_idx, sym in zip(deconv_indices, symbols):
            self.deconv_element_symbols_by_spectrum_idx[spec_idx] = sym

    def _parse_trt_spectrum(self, elem) -> Optional[Dict]:
        spec: Dict = {
            'name': elem.get('Name', 'Unknown'),
            'counts': [],
            'meta': {},
        }

        ch_el = elem.find('.//Channels')
        if ch_el is not None and ch_el.text:
            spec['counts'] = [int(float(v)) for v in ch_el.text.split(',') if v.strip()]

        for sub in elem.iter('ClassInstance'):
            st = sub.get('Type', '')
            if st == 'TRTSpectrumHeader':
                for tag in ('Date', 'Time', 'ChannelCount', 'CalibAbs', 'CalibLin',
                            'SigmaAbs', 'SigmaLin'):
                    el = sub.find(tag)
                    if el is not None and el.text:
                        spec['meta'][tag] = el.text.strip()

            elif st == 'TRTSpectrumHardwareHeader':
                for tag in ('RealTime', 'LifeTime', 'DeadTime',
                            'ZeroPeakPosition', 'ZeroPeakFrequency',
                            'PulseDensity', 'Amplification', 'ShapingTime',
                            'DetectorCount', 'SPVType', 'SPVRevision'):
                    el = sub.find(tag)
                    if el is not None and el.text:
                        spec['meta'][tag] = el.text.strip()

            elif st == 'TRTDetectorHeader':
                for tag in ('Type', 'DetectorName', 'WindowType'):
                    el = sub.find(tag)
                    if el is not None and el.text:
                        spec['meta'][f'Det_{tag}'] = el.text.strip()

            elif st == 'TRTESMAHeader':
                for tag in ('PrimaryEnergy', 'ElevationAngle', 'AzimutAngle'):
                    el = sub.find(tag)
                    if el is not None and el.text:
                        spec['meta'][tag] = el.text.strip()

        return spec if spec['counts'] else None

    # -- companion SEM .txt files -------------------------------------------

    def _load_companion_sem_txt(self):
        """Parse Hitachi SU-70 style SEM image .txt files next to the RTX."""
        rtx_dir = Path(self.filepath).parent
        for txt_path in sorted(rtx_dir.rglob('*.txt')):
            try:
                text = txt_path.read_text(errors='replace')
                if '[SemImageFile]' not in text:
                    continue
                for line in text.splitlines():
                    if '=' in line:
                        k, _, v = line.partition('=')
                        k = k.strip()
                        v = v.strip()
                        if v and k not in self.sem_metadata:
                            self.sem_metadata[k] = v
                break  # only need one representative file
            except Exception:
                continue

    # -- convenience accessors ---------------------------------------------

    def energy_calibration(self, idx: int = 0) -> Tuple[float, float]:
        """Return (offset_eV, gain_eV_per_channel)."""
        m = self.spectra[idx]['meta'] if self.spectra else {}
        off_keV = m.get('CalibAbs', self.metadata.get('calib_abs_keV', '0'))
        gain_keV = m.get('CalibLin', self.metadata.get('calib_lin_keV_per_ch', '0.01'))
        try:
            off_eV = float(off_keV) * 1000
        except ValueError:
            off_eV = 0.0
        try:
            gain_eV = float(gain_keV) * 1000
        except ValueError:
            gain_eV = 10.0
        return off_eV, gain_eV

    def beam_kV(self) -> float:
        kv = self.metadata.get('primary_energy_kV',
             self.metadata.get('beam_energy_kV', '0'))
        try:
            return float(kv)
        except ValueError:
            return 0.0

    def live_time_s(self, idx: int = 0) -> float:
        m = self.spectra[idx]['meta'] if self.spectra else {}
        v = m.get('LifeTime', self.metadata.get('live_time_ms', '0'))
        try:
            return float(v) / 1000
        except ValueError:
            return 0.0

    def real_time_s(self, idx: int = 0) -> float:
        m = self.spectra[idx]['meta'] if self.spectra else {}
        v = m.get('RealTime', self.metadata.get('real_time_ms', '0'))
        try:
            return float(v) / 1000
        except ValueError:
            return 0.0

    def _format_date_emsa(self) -> str:
        raw = self.metadata.get('acquisition_date',
              self.metadata.get('project_date', ''))
        for fmt in ('%d.%m.%Y', '%m/%d/%Y', '%Y-%m-%d', '%m.%d.%Y'):
            try:
                return datetime.strptime(raw, fmt).strftime('%d-%b-%Y')
            except ValueError:
                pass
        return raw

    def _format_time_emsa(self) -> str:
        """Return TIME in HH:MM format (EMSA spec; HyperSpy rejects seconds)."""
        raw = self.metadata.get('acquisition_time',
               self.metadata.get('project_time', ''))
        if not raw:
            return ''
        parts = raw.strip().replace('.', ':').split(':')
        if len(parts) >= 2:
            return f'{parts[0].zfill(2)}:{parts[1].zfill(2)}'
        return raw


# ---------------------------------------------------------------------------
# EMSA / MSA 1.0 exporter  (NIST DTSA II compatible)
# ---------------------------------------------------------------------------

class EMSAExporter:
    """Write a strictly spec-compliant EMSA/MSA 1.0 file.

    Tested against both NIST DTSA II and HyperSpy / HyperSpyUI.

    Key format rules enforced:
      - keyword field = '#' + 12-char left-justified tag + ': ' + value
      - CRLF line endings (\\r\\n)
      - Y data as one float-with-comma per line  (``value, ``)
      - ##COMMENT values must NOT contain ': ' to avoid parser split bug
      - all numeric keyword values written as plain floats (no sci notation)
    """

    def __init__(self, parser: RTXParser):
        self.p = parser

    @staticmethod
    def _safe_comment(text: str) -> str:
        """Sanitize comment text so it never contains ': ' which breaks
        the HyperSpy MSA parser (it does ``line.split(': ')`` without maxsplit)."""
        return text.replace(': ', ' - ')

    def export(self, path: str, idx: int = 0, title: str = None):
        if not self.p.spectra:
            raise ValueError("No spectra in parsed data")
        spec = self.p.spectra[idx]
        counts = spec['counts']
        off, gain = self.p.energy_calibration(idx)
        sm = spec['meta']

        CRLF = '\r\n'
        lines: List[str] = []

        def kv(tag, val):
            lines.append(f"#{tag:<12s}: {val}")

        # ---- Required keywords (must be first, in this order) ----
        kv('FORMAT',      'EMSA/MAS Spectral Data File')
        kv('VERSION',     '1.0')
        kv('TITLE',       title or spec['name'] or Path(self.p.filepath).stem)
        kv('DATE',        self.p._format_date_emsa())
        kv('TIME',        self.p._format_time_emsa())
        kv('OWNER',       self.p.metadata.get('project_creator', ''))
        kv('NPOINTS',     len(counts))
        kv('NCOLUMNS',    1)
        kv('XUNITS',      'eV')
        kv('YUNITS',      'counts')
        kv('DATATYPE',    'Y')
        kv('XPERCHAN',    f'{gain:.6f}')
        kv('OFFSET',      f'{off:.6f}')

        # ---- Optional keywords ----
        kv('SIGNALTYPE',  'EDS')

        bkv = self.p.beam_kV()
        if bkv:
            kv('BEAMKV -kV',  f'{bkv:.2f}')

        elev = sm.get('ElevationAngle', self.p.metadata.get('elevation_angle_deg', ''))
        if elev:
            kv('ELEVANGLE-dg', f'{float(elev):.2f}')

        azim = sm.get('AzimutAngle', self.p.metadata.get('azimuth_angle_deg', ''))
        if azim:
            kv('AZIMANGLE-dg', f'{float(azim):.2f}')

        lt = self.p.live_time_s(idx)
        rt = self.p.real_time_s(idx)
        if lt:
            kv('LIVETIME -s', f'{lt:.3f}')
        if rt:
            kv('REALTIME -s', f'{rt:.3f}')

        # Comments -- sanitize to avoid ': ' in value
        comment_parts = []
        det_name = sm.get('Det_DetectorName', '')
        det_type = sm.get('Det_Type', '')
        if det_name or det_type:
            comment_parts.append(f'Detector - {det_name} ({det_type})')

        spv = sm.get('SPVRevision', '')
        if spv:
            comment_parts.append(f'Pulse processor - {spv}')

        dt_pct = sm.get('DeadTime', self.p.metadata.get('dead_time_percent', ''))
        if dt_pct:
            comment_parts.append(f'Dead time {dt_pct}%')

        mag = self.p.metadata.get('magnification', '')
        if mag:
            comment_parts.append(f'Magnification {float(mag):.0f}x')

        wd = self.p.metadata.get('working_distance_mm', '')
        if wd:
            comment_parts.append(f'WD {float(wd):.1f} mm')

        sem = self.p.sem_metadata
        if sem.get('InstructName'):
            comment_parts.append(f'SEM {sem["InstructName"]}')
        if sem.get('SerialNumber'):
            comment_parts.append(f'S/N {sem["SerialNumber"]}')

        for c in comment_parts:
            kv('COMMENT',  self._safe_comment(c))

        # ---- Spectrum data ----
        kv('SPECTRUM',    'Spectral Data Starts Here')
        for c in counts:
            lines.append(f'{float(c):.1f}, ')
        kv('ENDOFDATA',   'End Of Data and File')

        with open(path, 'w', encoding='latin-1', newline='') as f:
            f.write(CRLF.join(lines))


# ---------------------------------------------------------------------------
# CSV exporter
# ---------------------------------------------------------------------------

class CSVExporter:
    def __init__(self, parser: RTXParser):
        self.p = parser

    def export(self, path: str, idx: int = 0):
        if not self.p.spectra:
            raise ValueError("No spectra in parsed data")
        spec = self.p.spectra[idx]
        counts = spec['counts']
        off, gain = self.p.energy_calibration(idx)

        with open(path, 'w') as f:
            f.write("Channel,Energy_eV,Energy_keV,Counts\n")
            for i, c in enumerate(counts):
                eV = off + i * gain
                f.write(f"{i},{eV:.2f},{eV/1000:.4f},{c}\n")


# ---------------------------------------------------------------------------
# Metadata text report
# ---------------------------------------------------------------------------

class MetadataExporter:
    def __init__(self, parser: RTXParser):
        self.p = parser

    def export(self, path: str, labels: List[str] = None):
        p = self.p
        with open(path, 'w') as f:
            w = f.write
            rule = '=' * 64
            dash = '-' * 48

            w(f"{rule}\n  BRUKER EDS INSTRUMENT & RUN REPORT\n{rule}\n\n")
            w(f"  Source file : {p.filepath}\n")
            w(f"  Generated  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

            # -- Acquisition --
            w(f"{dash}\n  ACQUISITION\n{dash}\n")
            self._kv(f, 'Date', p.metadata.get('acquisition_date', p.metadata.get('project_date')))
            self._kv(f, 'Time', p.metadata.get('acquisition_time', p.metadata.get('project_time')))
            self._kv(f, 'Map date', p.metadata.get('map_date'))
            self._kv(f, 'Map time', p.metadata.get('map_time'))
            self._kv(f, 'Creator / Operator', p.metadata.get('project_creator'))

            # -- SEM --
            w(f"\n{dash}\n  SEM INSTRUMENT\n{dash}\n")
            sem = p.sem_metadata
            self._kv(f, 'Instrument', sem.get('InstructName'))
            self._kv(f, 'Serial number', sem.get('SerialNumber'))
            self._kv(f, 'Signal', sem.get('SignalName'))
            self._kv(f, 'Lens mode', sem.get('LensMode'))
            self._kv(f, 'Scan speed', sem.get('ScanSpeed'))
            self._kv(f, 'Accelerating voltage', sem.get('AcceleratingVoltage'))
            self._kv(f, 'Emission current', sem.get('EmissionCurrent'))
            self._kv(f, 'Condition string', sem.get('Condition'))

            # -- Beam / Geometry --
            w(f"\n{dash}\n  BEAM & GEOMETRY\n{dash}\n")
            bkv = p.beam_kV()
            self._kv(f, 'Beam energy (kV)', f'{bkv:.1f}' if bkv else None)
            self._kv(f, 'Working distance (mm)', p.metadata.get('working_distance_mm'))
            self._kv(f, 'Magnification', p.metadata.get('magnification'))
            self._kv(f, 'Elevation / take-off angle (deg)', p.metadata.get('elevation_angle_deg'))
            self._kv(f, 'Azimuth angle (deg)', p.metadata.get('azimuth_angle_deg'))

            # -- Detector --
            w(f"\n{dash}\n  EDS DETECTOR\n{dash}\n")
            if p.spectra:
                sm = p.spectra[0]['meta']
                self._kv(f, 'Detector type', sm.get('Det_Type'))
                self._kv(f, 'Detector name', sm.get('Det_DetectorName'))
                self._kv(f, 'Window type', sm.get('Det_WindowType'))
                self._kv(f, 'Detector count', sm.get('DetectorCount', p.metadata.get('detector_count')))
                self._kv(f, 'SPV type', sm.get('SPVType', p.metadata.get('spv_type')))
                self._kv(f, 'SPV / pulse processor', sm.get('SPVRevision', p.metadata.get('spv_revision')))
                self._kv(f, 'Amplification', sm.get('Amplification', p.metadata.get('amplification')))
                self._kv(f, 'Shaping time (ns)', sm.get('ShapingTime', p.metadata.get('shaping_time_ns')))

            # -- Timing --
            w(f"\n{dash}\n  TIMING\n{dash}\n")
            for i, sp in enumerate(p.spectra):
                lt = p.live_time_s(i)
                rt = p.real_time_s(i)
                dt = sp['meta'].get('DeadTime', p.metadata.get('dead_time_percent', ''))
                if len(p.spectra) > 1:
                    tag = labels[i] if labels else sp['name']
                    label = f'  [{tag}]'
                else:
                    label = ''
                self._kv(f, f'Real time (s){label}', f'{rt:.3f}' if rt else None)
                self._kv(f, f'Live time (s){label}', f'{lt:.3f}' if lt else None)
                self._kv(f, f'Dead time (%){label}', dt if dt else None)

            # -- Energy calibration --
            w(f"\n{dash}\n  ENERGY CALIBRATION\n{dash}\n")
            self._kv(f, 'Channel count', p.metadata.get('channel_count'))
            off_eV, gain_eV = p.energy_calibration()
            self._kv(f, 'Offset (eV)', f'{off_eV:.3f}')
            self._kv(f, 'Gain (eV / channel)', f'{gain_eV:.3f}')
            self._kv(f, 'CalibAbs (keV, raw)', p.metadata.get('calib_abs_keV'))
            self._kv(f, 'CalibLin (keV/ch, raw)', p.metadata.get('calib_lin_keV_per_ch'))
            self._kv(f, 'SigmaAbs', p.metadata.get('sigma_abs'))
            self._kv(f, 'SigmaLin', p.metadata.get('sigma_lin'))
            self._kv(f, 'Zero peak position', p.metadata.get('zero_peak_position'))
            self._kv(f, 'Zero peak frequency', p.metadata.get('zero_peak_frequency'))
            self._kv(f, 'Pulse density', p.metadata.get('pulse_density'))

            # -- Image / Map --
            w(f"\n{dash}\n  MAP / IMAGE\n{dash}\n")
            self._kv(f, 'Image width (px)', p.metadata.get('image_width_px'))
            self._kv(f, 'Image height (px)', p.metadata.get('image_height_px'))
            self._kv(f, 'X calibration (um/px)', p.metadata.get('x_calibration_um_per_px'))
            self._kv(f, 'Y calibration (um/px)', p.metadata.get('y_calibration_um_per_px'))

            # -- Stage (from SEM txt) --
            if any(sem.get(k) for k in ('StagePositionX','StagePositionY','StagePositionZ',
                                         'StagePositionR','StagePositionT')):
                w(f"\n{dash}\n  STAGE POSITION\n{dash}\n")
                self._kv(f, 'X', sem.get('StagePositionX'))
                self._kv(f, 'Y', sem.get('StagePositionY'))
                self._kv(f, 'Z', sem.get('StagePositionZ'))
                self._kv(f, 'R (rotation)', sem.get('StagePositionR'))
                self._kv(f, 'T (tilt)', sem.get('StagePositionT'))

            # -- Spectrum summary --
            w(f"\n{dash}\n  SPECTRA SUMMARY\n{dash}\n")
            w(f"  Spectra found: {len(p.spectra)}\n")
            for i, sp in enumerate(p.spectra):
                counts = sp['counts']
                off_eV, gain_eV = p.energy_calibration(i)
                max_ch = counts.index(max(counts)) if counts else 0
                max_eV = off_eV + max_ch * gain_eV
                spec_label = labels[i] if labels else sp['name']
                w(f"\n  Spectrum {i+1}: {spec_label}\n")
                w(f"    Channels       : {len(counts)}\n")
                w(f"    Total counts   : {sum(counts):,}\n")
                w(f"    Max channel    : {max(counts):,}  (ch {max_ch}, ~{max_eV:.0f} eV / {max_eV/1000:.2f} keV)\n")
                w(f"    Energy range   : {off_eV:.0f} .. {off_eV + len(counts)*gain_eV:.0f} eV\n")

            # -- raw metadata dump --
            w(f"\n{dash}\n  ALL EXTRACTED RTX METADATA (raw keys)\n{dash}\n")
            for k in sorted(p.metadata):
                v = p.metadata[k]
                if v:
                    w(f"  {k}: {v}\n")

            if sem:
                w(f"\n{dash}\n  ALL SEM .txt METADATA\n{dash}\n")
                for k in sorted(sem):
                    w(f"  {k}: {sem[k]}\n")

            w(f"\n{rule}\n  END OF REPORT\n{rule}\n")

    @staticmethod
    def _kv(f, label, value):
        if value:
            f.write(f"  {label:.<40s} {value}\n")
        else:
            f.write(f"  {label:.<40s} (not available)\n")


# ---------------------------------------------------------------------------
# Conversion driver
# ---------------------------------------------------------------------------

def convert_rtx_file(rtx_path: str, output_dir: str = None) -> bool:
    rtx_path = Path(rtx_path)
    if not rtx_path.exists():
        print(f"  ERROR: file not found – {rtx_path}")
        return False

    out = Path(output_dir) if output_dir else rtx_path.parent / 'converted'
    out.mkdir(parents=True, exist_ok=True)
    stem = rtx_path.stem

    print(f"  Parsing  : {rtx_path}")
    parser = RTXParser(str(rtx_path))
    if not parser.parse():
        return False

    print(f"  Spectra  : {len(parser.spectra)} found")
    if not parser.spectra:
        print(f"  WARNING  : no EDS spectrum data in this file – skipping exports")
        # Still write the metadata report
        meta_path = out / f'{stem}_metadata.txt'
        MetadataExporter(parser).export(str(meta_path))
        print(f"  Created  : {meta_path.name}")
        return True

    # Build informative per-spectrum labels using canonical type names so
    # filenames clearly distinguish spot, line, deconv_spot, deconv_line,
    # background, calibration, sum, and empty spectra.
    # Multi-instance types get zero-padded sequential numbers (001, 002...).
    
    # First pass: build context for inferring deconv types
    index_map = _build_index_type_map(parser.spectra)
    dominant_type = _get_dominant_spectrum_type(parser.spectra)
    
    labels: List[str] = []
    seen: Set[str] = set()
    type_counts: Dict[str, int] = {}
    for i, sp in enumerate(parser.spectra):
        deconv_symbol = parser.deconv_element_symbols_by_spectrum_idx.get(i)
        label = _build_spectrum_label(
            sp.get('name', ''),
            sp.get('counts', []),
            i,
            index_map,
            dominant_type,
            deconv_symbol,
        )
        label = _deduplicate_label(label, seen, type_counts)
        labels.append(label)

    for i, sp in enumerate(parser.spectra):
        # Always include the descriptive suffix so every MSA/CSV file
        # is self-describing (spot vs line vs background, etc.).
        suffix = f'_{labels[i]}'

        # EMSA / MSA
        msa_path = out / f'{stem}{suffix}.msa'
        try:
            EMSAExporter(parser).export(str(msa_path), i, title=labels[i])
            print(f"  Created  : {msa_path.name}  ({len(sp['counts'])} channels, EMSA/MSA 1.0)")
        except Exception as e:
            print(f"  WARNING  : EMSA export failed – {e}")

        # CSV
        csv_path = out / f'{stem}{suffix}.csv'
        try:
            CSVExporter(parser).export(str(csv_path), i)
            print(f"  Created  : {csv_path.name}")
        except Exception as e:
            print(f"  WARNING  : CSV export failed – {e}")

    # Metadata report (one per RTX file)
    meta_path = out / f'{stem}_metadata.txt'
    MetadataExporter(parser).export(str(meta_path), labels=labels)
    print(f"  Created  : {meta_path.name}")

    return True


def batch_convert(input_dir: str, output_dir: str = None):
    inp = Path(input_dir)
    if not inp.exists():
        print(f"ERROR: directory not found – {inp}")
        return

    rtx_files = sorted(inp.rglob('*.rtx'))
    if not rtx_files:
        print(f"No .rtx files found under {inp}")
        return

    print(f"Found {len(rtx_files)} RTX file(s)\n")
    ok = fail = 0
    for f in rtx_files:
        print('-' * 60)
        if convert_rtx_file(str(f), output_dir):
            ok += 1
        else:
            fail += 1
        print()

    print('=' * 60)
    print(f"Done – {ok} converted, {fail} failed")


def main():
    ap = argparse.ArgumentParser(
        description="Convert Bruker EDS .rtx files to EMSA/MSA (DTSA II), CSV, and metadata text")
    ap.add_argument('input', help='RTX file or directory to process')
    ap.add_argument('-o', '--output', help='Output directory (default: <input_dir>/converted)')
    args = ap.parse_args()

    p = Path(args.input)
    if p.is_dir():
        batch_convert(args.input, args.output)
    else:
        convert_rtx_file(args.input, args.output)


if __name__ == '__main__':
    main()
