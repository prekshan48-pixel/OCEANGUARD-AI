from __future__ import annotations

import argparse
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from oceanguard_ai.sar_pipeline import (
    InputValidationError,
    ProcessingError,
    ProductMetadata,
    _snap_stages,
    _validate_geojson,
    inspect_product,
    parse_region,
    process_product,
)


def manifest(product_type: str = "GRD", mode: str = "IW") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <xfdu:XFDU xmlns:xfdu="urn:ccsds:schema:xfdu:1"
      xmlns:s1sarl1="urn:s1">
      <metadataSection>
        <metadataObject ID="platform">
          <metadataWrap><xmlData><safe:platform xmlns:safe="urn:safe">
            <safe:familyName>SENTINEL-1</safe:familyName>
            <safe:number>A</safe:number>
            <safe:instrument>
              <s1sarl1:missionId>S1A</s1sarl1:missionId>
              <s1sarl1:mode>{mode}</s1sarl1:mode>
              <s1sarl1:productType>{product_type}</s1sarl1:productType>
              <s1sarl1:transmitterReceiverPolarisation>VV</s1sarl1:transmitterReceiverPolarisation>
              <s1sarl1:transmitterReceiverPolarisation>VH</s1sarl1:transmitterReceiverPolarisation>
            </safe:instrument>
          </safe:platform></xmlData></metadataWrap>
        </metadataObject>
      </metadataSection>
    </xfdu:XFDU>"""


class ProductInspectionTests(unittest.TestCase):
    def test_inspects_safe_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "S1A_IW_GRDH_001.SAFE"
            source.mkdir()
            (source / "manifest.safe").write_text(manifest(), encoding="utf-8")

            metadata = inspect_product(source)

        self.assertEqual(metadata.format, "SAFE")
        self.assertEqual(metadata.mission, "S1A")
        self.assertEqual(metadata.product_type, "GRD")
        self.assertEqual(metadata.mode, "IW")
        self.assertEqual(metadata.polarizations, ("VV", "VH"))

    def test_inspects_safe_zip_without_extracting_or_modifying_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "sample.zip"
            original = manifest("SLC")
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("S1A_SAMPLE.SAFE/manifest.safe", original)

            metadata = inspect_product(source)

            with zipfile.ZipFile(source) as archive:
                self.assertEqual(archive.read("S1A_SAMPLE.SAFE/manifest.safe").decode(), original)

        self.assertEqual(metadata.product_type, "SLC")
        self.assertEqual(metadata.format, "SAFE")

    def test_rejects_unsupported_safe_product_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "manifest.safe"
            source.write_text(manifest("OCN"), encoding="utf-8")
            with self.assertRaisesRegex(InputValidationError, "supports SAFE GRD and SLC only"):
                inspect_product(source.parent)

    def test_rejects_missing_input(self) -> None:
        with self.assertRaisesRegex(InputValidationError, "Input does not exist"):
            inspect_product("missing.SAFE")

    def test_rejects_ordinary_rgb_image_without_treating_it_as_sar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "photo.jpg"
            source.write_bytes(b"ordinary RGB image")
            with self.assertRaisesRegex(InputValidationError, "Unsupported input"):
                inspect_product(source)


class ProcessingPlanTests(unittest.TestCase):
    def test_grd_plan_removes_grd_noise_and_calibrates(self) -> None:
        metadata = ProductMetadata(
            path="sample.SAFE",
            format="SAFE",
            mission="S1A",
            product_type="GRD",
            mode="IW",
            polarizations=("VV", "VH"),
        )

        stages = _snap_stages(metadata)
        names = [name for name, _ in stages]

        self.assertEqual(
            names,
            ["Apply-Orbit-File", "Remove-GRD-Border-Noise", "ThermalNoiseRemoval", "Calibration"],
        )
        calibration = stages[-1][1]
        self.assertEqual(calibration["outputSigmaBand"], "true")
        self.assertEqual(calibration["outputImageScaleInDb"], "true")

    def test_slc_plan_skips_grd_noise_and_debursts_tops(self) -> None:
        metadata = ProductMetadata(
            path="sample.SAFE",
            format="SAFE",
            mission="S1A",
            product_type="SLC",
            mode="IW",
            polarizations=("VV",),
        )

        names = [name for name, _ in _snap_stages(metadata)]

        self.assertEqual(names, ["Apply-Orbit-File", "Calibration", "TOPSAR-Deburst"])
        self.assertNotIn("ThermalNoiseRemoval", names)

    def test_slc_stripmap_does_not_add_tops_deburst(self) -> None:
        metadata = ProductMetadata(
            path="sample.SAFE",
            format="SAFE",
            mission="S1A",
            product_type="SLC",
            mode="SM",
            polarizations=("VV",),
        )
        self.assertNotIn("TOPSAR-Deburst", [name for name, _ in _snap_stages(metadata)])

    def test_region_requires_valid_wgs84_bounds(self) -> None:
        self.assertEqual(parse_region("72,10,74,12"), (72.0, 10.0, 74.0, 12.0))
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_region("74,10,72,12")
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_region("west,south,east,north")

    def test_land_mask_requires_supported_geojson_crs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "land.geojson"
            source.write_text(
                '{"type":"Feature","geometry":{"type":"Polygon","coordinates":[]},'
                '"properties":{},"crs":{"type":"name","properties":{"name":"EPSG:3857"}}}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(InputValidationError, "must use EPSG:4326"):
                _validate_geojson(source)

    def test_snap_failure_names_stage_and_preserves_original(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "S1A_SAMPLE.SAFE"
            source.mkdir()
            manifest_path = source / "manifest.safe"
            original = manifest()
            manifest_path.write_text(original, encoding="utf-8")
            output = Path(temporary) / "processed.tif"
            failure = subprocess.CompletedProcess(
                args=["gpt"],
                returncode=3,
                stdout="",
                stderr="orbit file unavailable",
            )
            with patch("oceanguard_ai.sar_pipeline.shutil.which", return_value="gpt"):
                with patch("oceanguard_ai.sar_pipeline.subprocess.run", return_value=failure):
                    with self.assertRaisesRegex(ProcessingError, "(?s)Apply-Orbit-File.*orbit file unavailable"):
                        process_product(source, output)

            self.assertEqual(manifest_path.read_text(encoding="utf-8"), original)
            self.assertFalse(output.exists())

    def test_does_not_overwrite_existing_output_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "S1A_SAMPLE.SAFE"
            source.mkdir()
            (source / "manifest.safe").write_text(manifest(), encoding="utf-8")
            output = Path(temporary) / "existing.tif"
            output.write_bytes(b"keep")

            with self.assertRaisesRegex(InputValidationError, "Output already exists"):
                process_product(source, output)

            self.assertEqual(output.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
