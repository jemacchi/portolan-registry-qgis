# Use

Open the panel from **Web > Portolan Registry** or the toolbar button.

## Find a catalog

The top list shows every catalog in the registry, with its status, collection count, and size. Filter it by text, by status, or to catalogs that overlap the map extent.

Pick a catalog to see its tree. Expand a node to read its children. Pick a node to see its description, license, thumbnail, and assets.

## Add data to the map

Select one or more assets, then select **Add to map**. A double-click on an asset adds it too.

| Asset | Layer |
|---|---|
| PMTiles link or asset | Vector tile layer, with the style chosen under **Style** |
| GeoParquet | Memory layer, read with DuckDB |
| COG | Raster layer, read over HTTP |
| GeoJSON, FlatGeobuf | Vector layer, read over HTTP |

**Style** lists the collection's MapLibre styles, with the default style first. **QGIS default style** skips them.

**Load GeoParquet in the map extent only** sends the map extent to DuckDB. When a file has a GeoParquet `bbox` column, DuckDB uses its statistics to skip row groups outside the extent. Every read stops at 1,000,000 features.

PMTiles layers read through a loopback server that the plugin runs on `127.0.0.1`. A saved project reconnects them when it reopens.

## Download

**Download selected** saves the selected assets. **Download all** saves the chosen node and everything below it, after it shows the file count and the known size.

The plugin saves each file in the folder you pick, at the same relative path as in the catalog, so the copy opens as a local STAC catalog. After each download, the plugin checks the file against its `file:checksum` when the catalog gives one. A second run skips files that are already complete, so it resumes an interrupted download.

## Use another registry

The plugin reads the Portolan registry export at `exports/catalogs.json`. To point it at a mirror, set the QGIS setting `PortolanRegistry/registry_url` in **Settings > Options > Advanced** and reopen the panel.
