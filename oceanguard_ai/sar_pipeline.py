"""Metadata-checked Sentinel-1 SAFE preprocessing with ESA SNAP GPT."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

LOGGER = logging.getLogger("oceanguard.sar")
SUPPORTED_SAFE_TYPES = {"GRD", "SLC"}
POLARIZATION_PATTERN = re.compile(r"\b(VV|VH|HH|HV)\b", re.IGNORECASE)
NODATA_VALUE = -9999.0


class PipelineError(Exception):
    """Base class for expected input and processing errors."""


class InputValidationError(PipelineError):
    """Raised when an input is missing, invalid, or unsupported."""


class ProcessingError(PipelineError):
    """Raised when SNAP or a post-processing validation stage fails."""


@dataclass(frozen=True)
class ProductMetadata:
    path: str
    format: str
    mission: str | None
    product_type: str
    mode: str | None
    polarizations: tuple[str, ...]
    start_time: str | None = None
    stop_time: str | None = None
    crs: str | None = None
    width: int | None = None
    height: int | None = None
    calibrated: bool = False
    terrain_corrected: bool = False

    @property
    def analysis_ready(self) -> bool:
        return (
            self.format == "GeoTIFF"
            and self.product_type in SUPPORTED_SAFE_TYPES
            and self.calibrated
            and self.terrain_corrected
            and self.crs is not None
            and bool(self.polarizations)
        )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1].casefold().replace("_", "").replace("-", "")


def _metadata_values(root: ET.Element, names: set[str]) -> list[str]:
    return [
        (element.text or "").strip()
        for element in root.iter()
        if _local_name(element.tag) in names and (element.text or "").strip()
    ]


def _first_metadata(root: ET.Element, *names: str) -> str | None:
    values = _metadata_values(root, {name.casefold() for name in names})
    return values[0] if values else None


def _parse_manifest(data: bytes, path: Path) -> ProductMetadata:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise InputValidationError(f"Invalid Sentinel-1 manifest.safe: {error}") from error

    product_type = _first_metadata(root, "productType")
    mission = _first_metadata(root, "missionId", "mission", "platform")
    mode = _first_metadata(root, "mode", "instrumentMode")
    if not product_type:
        raise InputValidationError("manifest.safe has no productType metadata; product type cannot be verified.")
    product_type = product_type.upper()
    if product_type not in SUPPORTED_SAFE_TYPES:
        raise InputValidationError(
            f"Sentinel-1 product type {product_type!r} is not supported. This pipeline supports SAFE GRD and SLC only."
        )
    if not mission or not re.search(r"\bS1[ABC]\b", mission.upper()):
        raise InputValidationError(
            "manifest.safe does not identify a Sentinel-1 mission (S1A, S1B, or S1C); refusing to process an unverified product."
        )

    polarization_values = _metadata_values(
        root,
        {"transmitterreceiverpolarisation", "transmitterreceiverpolarization", "polarisation", "polarization"},
    )
    polarizations: list[str] = []
    for value in polarization_values:
        for polarization in POLARIZATION_PATTERN.findall(value.upper()):
            if polarization not in polarizations:
                polarizations.append(polarization)
    if not polarizations:
        raise InputValidationError("manifest.safe does not declare a supported transmit/receive polarization.")

    return ProductMetadata(
        path=str(path),
        format="SAFE",
        mission=mission,
        product_type=product_type,
        mode=mode.upper() if mode else None,
        polarizations=tuple(polarizations),
        start_time=_first_metadata(root, "startTime"),
        stop_time=_first_metadata(root, "stopTime"),
    )


def _read_safe_manifest(path: Path) -> bytes:
    if path.is_dir():
        manifests = [child for child in path.iterdir() if child.name.casefold() == "manifest.safe"]
        if len(manifests) != 1:
            raise InputValidationError(
                f"Expected one manifest.safe at the root of {path}; found {len(manifests)}."
            )
        try:
            return manifests[0].read_bytes()
        except OSError as error:
            raise InputValidationError(f"Cannot read {manifests[0]}: {error}") from error

    if path.suffix.casefold() != ".zip" or not zipfile.is_zipfile(path):
        raise InputValidationError(f"{path} is not a Sentinel-1 SAFE directory or a valid SAFE ZIP archive.")
    try:
        with zipfile.ZipFile(path) as archive:
            manifests = [
                item for item in archive.namelist()
                if Path(item.replace("\\", "/")).name.casefold() == "manifest.safe"
            ]
            if len(manifests) != 1:
                raise InputValidationError(
                    f"Expected one manifest.safe in {path}; found {len(manifests)}."
                )
            return archive.read(manifests[0])
    except (OSError, zipfile.BadZipFile, KeyError) as error:
        raise InputValidationError(f"Cannot read SAFE archive {path}: {error}") from error


def _normalized_tags(tags: dict[str, Any]) -> dict[str, str]:
    return {str(key).casefold().replace("-", "_"): str(value).strip() for key, value in tags.items()}


def _inspect_geotiff(path: Path) -> ProductMetadata:
    try:
        import rasterio
    except ImportError as error:
        raise InputValidationError(
            "Inspecting GeoTIFF metadata requires Rasterio. Install requirements-sar.txt before inspecting this input."
        ) from error

    try:
        with rasterio.open(path) as dataset:
            tags = _normalized_tags(dataset.tags())
            for band_index in range(1, dataset.count + 1):
                tags.update(_normalized_tags(dataset.tags(band_index)))
            descriptions = " ".join(description or "" for description in dataset.descriptions)
            polarization_text = " ".join(
                value for key, value in tags.items() if "polar" in key
            ) + " " + descriptions
            polarizations = tuple(dict.fromkeys(match.upper() for match in POLARIZATION_PATTERN.findall(polarization_text)))
            mission_text = " ".join(
                value for key, value in tags.items() if key in {"sensor", "platform", "mission", "satellite"}
            )
            if not re.search(r"(?:sentinel[\s_-]*1|\bS1[ABC]\b)", mission_text, re.IGNORECASE):
                raise InputValidationError(
                    "GeoTIFF metadata does not identify Sentinel-1 SAR data. Ordinary RGB imagery is not accepted as preprocessed SAR."
                )

            product_type = next(
                (tags[key].upper() for key in ("product_type", "producttype", "product") if key in tags),
                "",
            )
            calibration_text = " ".join(
                tags[key].casefold()
                for key in ("calibration", "radiometric_calibration", "radiometric_quantity", "backscatter_quantity", "quantity")
                if key in tags
            )
            calibrated = any(
                quantity in calibration_text
                for quantity in ("sigma0", "sigma_0", "gamma0", "gamma_0", "beta0", "beta_0")
            ) or tags.get("calibration_applied", "").casefold() in {"true", "yes", "1"}
            terrain_corrected = any(
                tags.get(key, "").casefold() in {"true", "yes", "1", "rtc", "terrain-corrected"}
                for key in ("terrain_corrected", "orthorectified", "terrain_correction")
            ) or tags.get("processing_level", "").casefold() in {"rtc", "analysis-ready", "ard"}
            return ProductMetadata(
                path=str(path),
                format="GeoTIFF",
                mission=mission_text or None,
                product_type=product_type or "unknown",
                mode=tags.get("mode"),
                polarizations=polarizations,
                crs=dataset.crs.to_string() if dataset.crs else None,
                width=dataset.width,
                height=dataset.height,
                calibrated=calibrated,
                terrain_corrected=terrain_corrected,
            )
    except InputValidationError:
        raise
    except Exception as error:
        raise InputValidationError(f"Cannot inspect GeoTIFF metadata for {path}: {error}") from error


def inspect_product(source: str | Path) -> ProductMetadata:
    """Inspect product type and metadata without modifying the source."""
    path = Path(source).expanduser().resolve()
    if not path.exists():
        raise InputValidationError(f"Input does not exist: {path}")
    if path.is_dir() or path.suffix.casefold() == ".zip":
        return _parse_manifest(_read_safe_manifest(path), path)
    if path.suffix.casefold() in {".tif", ".tiff"}:
        return _inspect_geotiff(path)
    raise InputValidationError(
        f"Unsupported input {path.name!r}. Provide a Sentinel-1 SAFE directory/archive or a metadata-verified SAR GeoTIFF."
    )


def parse_region(value: str) -> tuple[float, float, float, float]:
    try:
        west, south, east, north = (float(part.strip()) for part in value.split(","))
    except (ValueError, TypeError) as error:
        raise argparse.ArgumentTypeError(
            "Region must contain west,south,east,north coordinates in EPSG:4326."
        ) from error
    if not all(math.isfinite(part) for part in (west, south, east, north)):
        raise argparse.ArgumentTypeError("Region coordinates must be finite numbers.")
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise argparse.ArgumentTypeError(
            "Region bounds must satisfy -180 <= west < east <= 180 and -90 <= south < north <= 90."
        )
    return west, south, east, north


def _operator_parameters(parent: ET.Element, parameters: dict[str, str]) -> None:
    params = ET.SubElement(parent, "parameters")
    for name, value in parameters.items():
        ET.SubElement(params, name).text = value


def _write_graph(
    path: Path,
    source: Path,
    operation: str | None,
    target: Path,
    *,
    operator_parameters: dict[str, str] | None = None,
    target_format: str = "BEAM-DIMAP",
) -> None:
    graph = ET.Element("graph", {"id": "OceanGuardSentinel1Preprocessing"})
    ET.SubElement(graph, "version").text = "1.0"
    read = ET.SubElement(graph, "node", {"id": "Read"})
    ET.SubElement(read, "operator").text = "Read"
    _operator_parameters(read, {"file": str(source)})
    if operation is not None:
        process = ET.SubElement(graph, "node", {"id": operation})
        ET.SubElement(process, "operator").text = operation
        sources = ET.SubElement(process, "sources")
        ET.SubElement(sources, "sourceProduct", {"refid": "Read"})
        if operator_parameters:
            _operator_parameters(process, operator_parameters)
    write = ET.SubElement(graph, "node", {"id": "Write"})
    ET.SubElement(write, "operator").text = "Write"
    write_sources = ET.SubElement(write, "sources")
    ET.SubElement(write_sources, "sourceProduct", {"refid": operation or "Read"})
    _operator_parameters(write, {"file": str(target), "formatName": target_format})
    ET.ElementTree(graph).write(path, encoding="utf-8", xml_declaration=True)


def _dimap_data_path(dim_path: Path) -> Path:
    return dim_path.with_suffix(".data")


def _validate_dimap(path: Path, stage: str) -> None:
    data_folder = _dimap_data_path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise ProcessingError(f"SNAP stage {stage!r} did not produce a non-empty DIMAP product: {path}")
    if not data_folder.is_dir() or not any(data_folder.iterdir()):
        raise ProcessingError(f"SNAP stage {stage!r} produced no DIMAP raster data: {data_folder}")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as error:
        raise ProcessingError(f"SNAP stage {stage!r} produced invalid DIMAP metadata: {error}") from error
    if not any(_local_name(element.tag) == "rasterdimensions" for element in root.iter()):
        raise ProcessingError(f"SNAP stage {stage!r} output has no raster-dimension metadata.")


def _validate_geotiff(path: Path) -> None:
    try:
        import rasterio
    except ImportError as error:
        raise ProcessingError("Validating the georeferenced output requires Rasterio; install requirements-sar.txt.") from error
    try:
        with rasterio.open(path) as dataset:
            if dataset.count < 1 or dataset.width < 1 or dataset.height < 1:
                raise ProcessingError(f"Output GeoTIFF has invalid raster dimensions: {path}")
            if dataset.crs is None:
                raise ProcessingError(f"Output GeoTIFF has no coordinate reference system: {path}")
            if dataset.transform.is_identity:
                raise ProcessingError(f"Output GeoTIFF has no geographic transform: {path}")
            radiometry = " ".join(
                value.casefold()
                for tags in (
                    dataset.tags(),
                    *(dataset.tags(index) for index in range(1, dataset.count + 1)),
                )
                for key, value in tags.items()
                if any(label in key.casefold() for label in ("quantity", "calibration", "backscatter"))
            )
            if (
                not any("sigma0" in (name or "").casefold() for name in dataset.descriptions)
                and "sigma0" not in radiometry
                and "sigma_0" not in radiometry
            ):
                raise ProcessingError(
                    f"Output GeoTIFF has no calibrated Sigma0 band description: {path}"
                )
    except ProcessingError:
        raise
    except Exception as error:
        raise ProcessingError(f"Cannot validate output GeoTIFF {path}: {error}") from error


def _validate_geojson(path: Path) -> list[dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InputValidationError(f"Cannot read land-mask GeoJSON {path}: {error}") from error
    if not isinstance(document, dict):
        raise InputValidationError("Land-mask GeoJSON root must be an object.")
    crs = document.get("crs")
    if crs:
        properties = crs.get("properties", {}) if isinstance(crs, dict) else {}
        crs_name = str(properties.get("name", "")).upper() if isinstance(properties, dict) else ""
        if not any(value in crs_name for value in ("4326", "CRS84", "WGS84")):
            raise InputValidationError("Land-mask GeoJSON must use EPSG:4326 (RFC 7946 longitude/latitude coordinates).")
    kind = document.get("type")
    if kind == "FeatureCollection":
        features = document.get("features", [])
        if not isinstance(features, list):
            raise InputValidationError("GeoJSON FeatureCollection features must be a list.")
    elif kind == "Feature":
        features = [document]
    elif kind in {"Polygon", "MultiPolygon"}:
        features = [{"geometry": document}]
    else:
        raise InputValidationError("Land-mask file must contain a GeoJSON Polygon or MultiPolygon geometry.")
    geometries = [
        feature.get("geometry")
        for feature in features
        if isinstance(feature, dict)
        and isinstance(feature.get("geometry"), dict)
        and feature["geometry"].get("type") in {"Polygon", "MultiPolygon"}
    ]
    if not geometries:
        raise InputValidationError("Land-mask GeoJSON contains no Polygon or MultiPolygon geometries.")
    return geometries


def _apply_land_mask(source: Path, target: Path, geojson: Path) -> None:
    try:
        import rasterio
        from rasterio.features import geometry_mask
        from rasterio.warp import transform_geom
    except ImportError as error:
        raise ProcessingError("Applying a vector land mask requires Rasterio; install requirements-sar.txt.") from error

    geometries = _validate_geojson(geojson)
    try:
        with rasterio.open(source) as src:
            if src.crs is None:
                raise ProcessingError("Cannot apply a land mask to a raster without a coordinate reference system.")
            profile = src.profile.copy()
            profile.update(dtype="float32", nodata=NODATA_VALUE, compress="deflate")
            output_geometries = [
                transform_geom("EPSG:4326", src.crs, geometry)
                for geometry in geometries
            ]
            with rasterio.open(target, "w", **profile) as dst:
                dst.update_tags(**src.tags())
                dst.update_tags(LAND_MASK_APPLIED="true", LAND_MASK_SOURCE=geojson.name)
                for band_index, description in enumerate(src.descriptions, start=1):
                    if description:
                        dst.set_band_description(band_index, description)
                    band_tags = src.tags(band_index)
                    if band_tags:
                        dst.update_tags(band_index, **band_tags)
                for _, window in src.block_windows(1):
                    values = src.read(window=window, out_dtype="float32")
                    mask = geometry_mask(
                        output_geometries,
                        out_shape=(int(window.height), int(window.width)),
                        transform=src.window_transform(window),
                        invert=True,
                    )
                    values[:, mask] = NODATA_VALUE
                    dst.write(values, window=window)
    except (InputValidationError, ProcessingError):
        raise
    except Exception as error:
        raise ProcessingError(f"Land-mask stage failed for {geojson}: {error}") from error


def _run_gpt(gpt: str, graph: Path, stage: str, timeout: float) -> None:
    try:
        result = subprocess.run(
            [gpt, str(graph)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ProcessingError(
            f"SNAP stage {stage!r} exceeded the {timeout:g}-second timeout. "
            "Retry with a smaller --region or increase --timeout."
        ) from error
    except OSError as error:
        raise ProcessingError(f"Could not start SNAP GPT for stage {stage!r}: {error}") from error
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "SNAP returned no diagnostic output").strip()
        excerpt = details[-4000:]
        raise ProcessingError(
            f"SNAP stage {stage!r} failed with exit code {result.returncode}.\n{excerpt}"
        )


def _snap_stages(metadata: ProductMetadata) -> list[tuple[str, dict[str, str]]]:
    polarizations = ",".join(metadata.polarizations)
    stages: list[tuple[str, dict[str, str]]] = [
        (
            "Apply-Orbit-File",
            {
                "orbitType": "Sentinel Precise (Auto Download)",
                "polyDegree": "3",
                "continueOnFail": "false",
            },
        )
    ]
    if metadata.product_type == "GRD":
        stages.append(("Remove-GRD-Border-Noise", {}))
        stages.append(
            (
                "ThermalNoiseRemoval",
                {
                    "selectedPolarisations": polarizations,
                    "removeThermalNoise": "true",
                    "removeThermalNoiseFromProducts": "false",
                    "reIntroduceThermalNoise": "false",
                },
            )
        )
    stages.append(
        (
            "Calibration",
            {
                "outputSigmaBand": "true",
                "outputBetaBand": "false",
                "outputGammaBand": "false",
                "selectedPolarisations": polarizations,
                "auxiliaryFile": "Latest Auxiliary File",
                "outputImageScaleInDb": "true",
            },
        )
    )
    if metadata.product_type == "SLC" and metadata.mode in {"IW", "EW"}:
        stages.append(("TOPSAR-Deburst", {}))
    return stages


def _region_wkt(region: tuple[float, float, float, float]) -> str:
    west, south, east, north = region
    return f"POLYGON(({west} {south},{east} {south},{east} {north},{west} {north},{west} {south}))"


def _snap_process(
    metadata: ProductMetadata,
    source: Path,
    output: Path,
    *,
    gpt: str,
    region: tuple[float, float, float, float] | None,
    dem: str,
    timeout: float,
) -> None:
    with tempfile.TemporaryDirectory(prefix="oceanguard-s1-", dir=output.parent) as working:
        workdir = Path(working)
        current_source = source
        for index, (operator, parameters) in enumerate(_snap_stages(metadata), start=1):
            stage_name = operator
            target = workdir / f"{index:02d}-{operator}.dim"
            graph = workdir / f"{index:02d}-{operator}.xml"
            _write_graph(graph, current_source, operator, target, operator_parameters=parameters)
            LOGGER.info("Running SNAP stage %s", stage_name)
            _run_gpt(gpt, graph, stage_name, timeout)
            _validate_dimap(target, stage_name)
            current_source = target

        if region is not None:
            subset_path = workdir / "region-subset.dim"
            subset_graph = workdir / "region-subset.xml"
            _write_graph(
                subset_graph,
                current_source,
                "Subset",
                subset_path,
                operator_parameters={"geoRegion": _region_wkt(region), "copyMetadata": "true"},
            )
            LOGGER.info("Running SNAP stage Subset (test region)")
            _run_gpt(gpt, subset_graph, "Subset (test region)", timeout)
            _validate_dimap(subset_path, "Subset (test region)")
            current_source = subset_path

        corrected_path = workdir / "terrain-corrected.dim"
        correction_graph = workdir / "terrain-correction.xml"
        _write_graph(
            correction_graph,
            current_source,
            "Terrain-Correction",
            corrected_path,
            operator_parameters={
                "demName": dem,
                "demResamplingMethod": "BILINEAR_INTERPOLATION",
                "imgResamplingMethod": "BILINEAR_INTERPOLATION",
                "pixelSpacingInDegree": "0.0001",
                "mapProjection": "EPSG:4326",
                "nodataValueAtSea": "false",
                "saveDEM": "false",
                "saveLatLon": "false",
                "saveIncidenceAngleFromEllipsoid": "false",
                "saveLocalIncidenceAngle": "false",
                "saveProjectedLocalIncidenceAngle": "false",
                "saveSelectedSourceBand": "true",
            },
        )
        LOGGER.info("Running SNAP stage Terrain-Correction (DEM: %s)", dem)
        _run_gpt(gpt, correction_graph, "Terrain-Correction", timeout)
        _validate_dimap(corrected_path, "Terrain-Correction")

        export_graph = workdir / "geotiff-export.xml"
        _write_graph(
            export_graph,
            corrected_path,
            None,
            output,
            target_format="GeoTIFF",
        )
        LOGGER.info("Writing georeferenced GeoTIFF")
        _run_gpt(gpt, export_graph, "GeoTIFF export", timeout)
        if not output.is_file() or output.stat().st_size == 0:
            raise ProcessingError("SNAP GeoTIFF export did not create a non-empty output file.")


def process_product(
    source: str | Path,
    output: str | Path,
    *,
    gpt_executable: str | None = None,
    region: tuple[float, float, float, float] | None = None,
    dem: str = "SRTM 1Sec HGT",
    land_mask: str | Path | None = None,
    timeout: float = 7200,
    overwrite: bool = False,
) -> Path:
    """Preprocess a Sentinel-1 SAFE product, or validate/copy verified analysis-ready GeoTIFF."""
    metadata = inspect_product(source)
    source_path = Path(metadata.path)
    output_path = Path(output).expanduser().resolve()
    if output_path.suffix.casefold() not in {".tif", ".tiff"}:
        raise InputValidationError("Output must use a .tif or .tiff extension.")
    if source_path == output_path or (source_path.is_dir() and source_path in output_path.parents):
        raise InputValidationError("Output must be outside the source product; original files are never overwritten.")
    if output_path.exists() and not overwrite:
        raise InputValidationError(f"Output already exists: {output_path}. Choose another path or pass --overwrite.")
    if not math.isfinite(timeout) or timeout <= 0:
        raise InputValidationError("Timeout must be greater than zero.")
    if region is not None:
        try:
            parse_region(",".join(str(value) for value in region))
        except argparse.ArgumentTypeError as error:
            raise InputValidationError(str(error)) from error

    executable = gpt_executable or shutil.which("gpt")
    if metadata.format == "SAFE" and not executable:
        raise ProcessingError(
            "ESA SNAP GPT was not found. Install SNAP with the Sentinel-1 toolbox and add gpt to PATH, "
            "or pass --gpt with the full executable path."
        )
    if metadata.format == "GeoTIFF" and not metadata.analysis_ready:
        raise InputValidationError(
            "GeoTIFF is not verifiably analysis-ready. Require Sentinel-1 metadata, declared radiometric "
            "calibration, polarization, a CRS, and terrain-correction metadata. Otherwise provide the original SAFE product."
        )
    mask_path = Path(land_mask).expanduser().resolve() if land_mask is not None else None
    if mask_path is not None:
        if not mask_path.is_file():
            raise InputValidationError(f"Land-mask GeoJSON does not exist: {mask_path}")
        _validate_geojson(mask_path)
        if metadata.format == "GeoTIFF":
            LOGGER.info("Applying the requested land mask to the verified analysis-ready GeoTIFF.")

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ProcessingError(f"Cannot create output directory {output_path.parent}: {error}") from error
    with tempfile.TemporaryDirectory(prefix="oceanguard-output-", dir=output_path.parent) as staging:
        staging_path = Path(staging)
        intermediate_output = staging_path / "processed.tif"
        if metadata.format == "SAFE":
            assert executable is not None
            _snap_process(
                metadata,
                source_path,
                intermediate_output,
                gpt=executable,
                region=region,
                dem=dem,
                timeout=timeout,
            )
            _validate_geotiff(intermediate_output)
        else:
            try:
                shutil.copy2(source_path, intermediate_output)
            except OSError as error:
                raise ProcessingError(f"Could not stage the analysis-ready input GeoTIFF: {error}") from error
            _validate_geotiff(intermediate_output)

        final_output = intermediate_output
        if mask_path is not None:
            masked_output = staging_path / "land-masked.tif"
            _apply_land_mask(intermediate_output, masked_output, mask_path)
            _validate_geotiff(masked_output)
            final_output = masked_output

        try:
            os.replace(final_output, output_path)
        except OSError as error:
            raise ProcessingError(f"Could not publish output GeoTIFF at {output_path}: {error}") from error
    LOGGER.info("Validated Sentinel-1 output: %s", output_path)
    return output_path


def _metadata_to_json(metadata: ProductMetadata) -> str:
    return json.dumps(asdict(metadata), indent=2)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oceanguard-sar",
        description="Inspect and preprocess metadata-verified Sentinel-1 products.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect", help="Inspect product type and available metadata.")
    inspect_parser.add_argument("input", type=Path)

    process_parser = subparsers.add_parser("process", help="Preprocess a Sentinel-1 SAFE product with ESA SNAP.")
    process_parser.add_argument("input", type=Path)
    process_parser.add_argument("--output", required=True, type=Path, help="Output GeoTIFF path.")
    process_parser.add_argument("--gpt", help="Path to ESA SNAP gpt executable; defaults to PATH lookup.")
    process_parser.add_argument(
        "--region",
        type=parse_region,
        metavar="W,S,E,N",
        help="Optional small test region in EPSG:4326 (west,south,east,north).",
    )
    process_parser.add_argument("--dem", default="SRTM 1Sec HGT", help="SNAP DEM name (default: SRTM 1Sec HGT).")
    process_parser.add_argument("--land-mask", type=Path, help="Optional EPSG:4326 GeoJSON land polygons to mask.")
    process_parser.add_argument("--timeout", type=float, default=7200, help="Per-stage timeout in seconds.")
    process_parser.add_argument("--overwrite", action="store_true", help="Replace an existing output file.")
    process_parser.add_argument("--verbose", action="store_true", help="Show stage progress and SNAP diagnostics.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if getattr(args, "verbose", False) else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )
    try:
        if args.command == "inspect":
            print(_metadata_to_json(inspect_product(args.input)))
            return 0
        output = process_product(
            args.input,
            args.output,
            gpt_executable=args.gpt,
            region=args.region,
            dem=args.dem,
            land_mask=args.land_mask,
            timeout=args.timeout,
            overwrite=args.overwrite,
        )
        print(f"Preprocessing complete: {output}")
        return 0
    except PipelineError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
