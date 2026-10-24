# OceanGuard AI

OceanGuard AI is a marine-pollution intelligence dashboard prototype. It demonstrates an incident review workflow, an interactive map, vessel-analysis examples, and a local drift-vector preview.

## Run locally

Open `index.html` in a modern browser. The map uses Leaflet and OpenStreetMap tiles loaded from their public CDNs, so those map layers require an internet connection. If the map library or imagery is unavailable, the dashboard displays a status message and keeps incident records accessible.

## Data and limitations

The records bundled with the dashboard are fictional interface demonstrations. No live satellite imagery, spill-detection model, AIS vessel feed, environmental observations, or validated drift model is connected. Timestamps and affected-area measurements are not provided and are shown as unavailable rather than inferred. Vessel scores and confidence values are illustrative only; they are not calibrated probabilities or evidence of wrongdoing. Geographic boundaries and tracks are conceptual examples, not official maritime boundaries or actual vessel movements.

Reports export only these bundled demonstration records as CSV. The drift page calculates a simple vector from user-entered example values; it is not a spill trajectory, forecast, or substitute for verified environmental data and validated models.

## Dashboard features

- Responsive marine-intelligence dashboard with Dashboard, Spill Detection, Incident History, Vessel Tracking, Drift Prediction, Reports, and Settings navigation.
- Summary metrics, incident tables, status filtering, and map navigation for the sample records.
- Interactive Leaflet map with clearly identified demonstration markers, tracks, and a conceptual review boundary.
- CSV export of the demonstration incident records.
- Accessible controls and explicit map loading and error messaging.

## Sentinel-1 SAR preprocessing

The optional Python pipeline in `oceanguard_ai/sar_pipeline.py` inspects product metadata before processing. It accepts original Sentinel-1 SAFE directories or ZIP archives with a readable `manifest.safe`, and supports GRD and SLC products. Ordinary RGB images and GeoTIFFs without verifiable Sentinel-1, calibration, polarization, terrain-correction, and CRS metadata are rejected; an image file extension alone is never treated as evidence of processed SAR.

### Dependencies and computational requirements

- Python 3.10 or newer.
- ESA SNAP with the Sentinel-1 Toolbox installed, including the `gpt` command-line executable on `PATH` (or pass its path with `--gpt`). SNAP is an external desktop application and is not installed by pip.
- Python output validation and optional land masking use Rasterio: `python -m pip install -r requirements-sar.txt`.
- SNAP may need network access to download precise orbit files and DEM tiles. Provide sufficient temporary storage for intermediate DIMAP products; the pipeline stores them beside the requested output and removes them when processing finishes. Large scenes can require several gigabytes of memory and disk space. Start with a small `--region` before processing full scenes.

### Inspect a product

```powershell
python -m oceanguard_ai.sar_pipeline inspect "C:\data\S1A_IW_GRDH_sample.SAFE"
```

The JSON report includes the detected input format, mission, product type, acquisition mode, polarizations, acquisition times (when present), and geospatial metadata for supported GeoTIFFs. Missing required SAFE metadata or unsupported product types produce an error instead of a guessed processing plan.

### Process a small region

```powershell
python -m oceanguard_ai.sar_pipeline process "C:\data\S1A_IW_GRDH_sample.SAFE" `
  --output "C:\data\preview.tif" `
  --region 72,10,74,12 `
  --verbose
```

`--region` is optional west,south,east,north bounds in EPSG:4326. Use it to validate a small area first. `--dem` selects a SNAP DEM (the default is `SRTM 1Sec HGT`); DEM coverage or download failures are reported by the failed terrain-correction stage. `--timeout` changes the per-stage timeout. Existing outputs are not replaced unless `--overwrite` is specified.

### Product-specific processing and outputs

- **GRD:** apply orbit file → remove GRD border noise → remove thermal noise → calibrate selected polarizations to Sigma0 in dB → optional geographic subset → terrain correction using the selected DEM → GeoTIFF export.
- **SLC:** apply orbit file → calibrate selected polarizations to Sigma0 in dB → TOPSAR deburst for IW/EW modes only → optional geographic subset → terrain correction using the selected DEM → GeoTIFF export. GRD-only border and thermal-noise removal operators are not blindly applied to SLC.
- Speckle filtering is intentionally omitted; it is detection-method-dependent and can change small-target texture. Land masking is optional and requires a user-supplied EPSG:4326 GeoJSON of land polygons (`--land-mask land.geojson`); it is applied after terrain correction. No coastline is invented or downloaded implicitly.
- Output is a georeferenced GeoTIFF with terrain-corrected Sigma0 bands. The pipeline verifies each intermediate DIMAP product, the final raster dimensions, CRS, geotransform, and calibrated-band metadata before moving the result to the requested output path. The input SAFE is read-only; intermediate files are temporary and cleaned up on success or failure.
- An existing GeoTIFF is accepted only as a verified analysis-ready pass-through when its metadata explicitly declares Sentinel-1, a supported product type, radiometric calibration, polarization, terrain correction, and CRS. It is not recalibrated or terrain-corrected a second time.

Tests use Python's standard-library `unittest` and do not require SNAP:

```powershell
python -m unittest discover -s tests -v
```

The tests cover product inspection, unsupported inputs, product-specific stage selection, region and mask validation, overwrite protection, and SNAP processing failures. A real processing run additionally requires SNAP and Rasterio as described above.
