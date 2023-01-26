"""stitching.py: utilities for combining interferograms into larger images."""
import itertools
import math
import re
import tempfile
from os import fspath
from pathlib import Path
from typing import List, Optional, Pattern, Tuple, Union

import numpy as np
from numpy.typing import DTypeLike
from osgeo import gdal, osr

from dolphin import io, utils
from dolphin._log import get_log
from dolphin._types import Filename

logger = get_log()


def merge_by_date(
    image_file_list: List[Filename],
    file_date_fmt: str = io.DEFAULT_DATETIME_FORMAT,
    output_dir: Filename = ".",
    driver: str = "ENVI",
):
    """Group images from the same date and merge into one image per date.

    Parameters
    ----------
    image_file_list : List[Filename]
        list of paths to images.
    file_date_fmt : Optional[str]
        format of the date in the filename. Default is %Y%m%d
    output_dir : Filename
        path to output directory
    driver : str
        GDAL driver to use for output. Default is ENVI.

    Returns
    -------
    dict
        key is the dates of the SLC acquisitions
        Value is the path to the stitched image

    Notes
    -----
    This function is intended to be used with filenames that contain date pairs
    (from interferograms).
    """
    grouped_images = group_by_date(image_file_list, file_date_fmt=file_date_fmt)
    stitched_acq_times = {}

    for dates, cur_images in grouped_images.items():
        logger.info(f"Stitching {len(cur_images)} images from {dates} into one image")
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        outfile = Path(output_dir) / (io._format_date_pair(*dates) + ".int")

        merge_images(
            cur_images,
            outfile=outfile,
            driver=driver,
        )

        stitched_acq_times[dates] = outfile

    return stitched_acq_times


def group_by_date(
    file_list: List[Filename], file_date_fmt: str = io.DEFAULT_DATETIME_FORMAT
):
    """Combine Sentinel objects by date.

    Parameters
    ----------
    file_list: List[Filename]
        path to folder containing CSLC files
    file_date_fmt: str
        format of the date in the filename.
        Default is [dolphin.io.DEFAULT_DATETIME_FORMAT][]

    Returns
    -------
    dict
        key is the date of the SLC acquisition
        Value is a list of Paths on that date:
        [(datetime.date(2017, 10, 13),
          [Path(...)
            Path(...),
            ...]),
         (datetime.date(2017, 10, 25),
          [Path(...)
            Path(...),
            ...]),
    """
    sorted_file_list, _ = utils.sort_files_by_date(
        file_list, file_date_fmt=file_date_fmt
    )

    # Now collapse into groups, sorted by the date
    grouped_images = {
        dates: list(g)
        for dates, g in itertools.groupby(
            sorted_file_list, key=lambda x: tuple(utils.get_dates(x))
        )
    }
    return grouped_images


def group_by_burst(
    file_list: List[Filename],
    burst_id_fmt: Union[str, Pattern[str]] = io.OPERA_BURST_RE,
):
    """Combine Sentinel objects by burst.

    Parameters
    ----------
    file_list: List[Filename]
        path to folder containing CSLC files
    burst_id_fmt: str
        format of the burst id in the filename.
        Default is [dolphin.io.OPERA_BURST_RE][]

    Returns
    -------
    dict
        key is the burst id of the SLC acquisition
        Value is a list of Paths on that burst:
        {
            't087_185678_iw2': [Path(...), Path(...),],
            't087_185678_iw3': [Path(...),... ],
        }
    """

    def get_burst_id(filename):
        m = re.search(burst_id_fmt, str(filename))
        if not m:
            raise ValueError(f"Could not parse burst id from {filename}")
        return m.group()

    def sort_by_burst_id(file_list):
        """Sort files by burst id."""
        burst_ids = [get_burst_id(f) for f in file_list]
        file_burst_tups = sorted(
            [(f, d) for f, d in zip(file_list, burst_ids)],
            # use the date or dates as the key
            key=lambda f_d_tuple: f_d_tuple[1],  # type: ignore
        )
        # Unpack the sorted pairs with new sorted values
        file_list, burst_ids = zip(*file_burst_tups)  # type: ignore
        return file_list

    sorted_file_list = sort_by_burst_id(file_list)
    # Now collapse into groups, sorted by the burst_id
    grouped_images = {
        burst_id: list(g)
        for burst_id, g in itertools.groupby(
            sorted_file_list, key=lambda x: get_burst_id(x)
        )
    }
    return grouped_images


def merge_images(
    file_list: List[Filename],
    outfile: Filename,
    target_aligned_pixels: bool = True,
    driver: str = "ENVI",
    out_nodata: Optional[float] = 0,
    out_dtype: Optional[DTypeLike] = None,
):
    """Combine multiple SLC images on the same date into one image.

    Parameters
    ----------
    file_list : List[Filename]
        list of raster filenames
    outfile : Filename
        path to output file
    output_dir : Filename
        path to output directory
    target_aligned_pixels: bool
        if True, adjust output image bounds so that pixel coordinates
        are integer multiples of pixel size, matching the ``-tap``
        options of GDAL utilities.
        Default is True.
    driver : str
        GDAL driver to use for output file. Default is ENVI.
    out_nodata : Optional[float]
        nodata value to use for output file. Default is 0.
    out_dtype : Optional[DTypeLike]
        output data type. Default is None, which will use the data type
        of the first image in the list.
    """
    if len(file_list) == 1:
        logger.info("Only one image, no stitching needed")
        logger.info(f"Copying {file_list[0]} to {outfile} and zeroing nodata values.")
        _nodata_to_zero(
            file_list[0],
            outfile=outfile,
            driver=driver,
        )
        return

    # Make sure all the files are in the same projection.
    projection = _get_mode_projection(file_list)
    res = _get_resolution(file_list)
    # If not, warp them to the most common projection using VRT files in a tempdir
    temp_dir = tempfile.TemporaryDirectory()
    # temp_dir = Path("test_ifgs")

    warped_file_list = _warp_to_projection(
        file_list,
        # temp_dir,
        Path(temp_dir.name),
        projection,
        res,
    )
    # Compute output array shape. We guarantee it will cover the output
    # bounds completely
    bounds, gt = _get_combined_bounds_gt(  # type: ignore
        *warped_file_list, target_aligned_pixels=target_aligned_pixels
    )

    out_shape = _get_output_shape(bounds, res)
    out_dtype = out_dtype or io.get_dtype(warped_file_list[0])

    io.write_arr(
        arr=None,
        output_name=outfile,
        driver=driver,
        nbands=1,
        shape=out_shape,
        dtype=out_dtype,
        nodata=out_nodata,
        geotransform=gt,
        projection=projection,
    )

    out_left, out_bottom, out_right, out_top = bounds
    # Now loop through the files and write them to the output
    for f in warped_file_list:
        logger.info(f"Stitching {f} into {outfile}")
        ds_in = gdal.Open(fspath(f))
        in_left, in_bottom, in_right, in_top = io.get_raster_bounds(ds=ds_in)

        # Get the spatial intersection of input and output
        int_right = min(in_right, out_right)
        int_top = min(in_top, out_top)
        int_left = max(in_left, out_left)
        int_bottom = max(in_bottom, out_bottom)

        # Get the pixel coordinates of the intersection in the input
        # For the offset (top-left), do a "floor" instead of "round"
        row_top, col_left = io.xy_to_rowcol(int_left, int_top, ds=ds_in, do_round=False)
        row_bottom, col_right = io.xy_to_rowcol(int_right, int_bottom, ds=ds_in)
        in_rows, in_cols = ds_in.RasterYSize, ds_in.RasterXSize
        # Read the input data in this window
        arr_in = ds_in.ReadAsArray(
            col_left,
            row_top,
            # Clip the width/height to the raster size
            min(col_right - col_left, in_cols),
            min(row_bottom - row_top, in_rows),
        )

        # Get pixel coordinates of the intersection in the output
        # For the offset (top-left), do a "floor" instead of "round"
        row_top, col_left = io.xy_to_rowcol(
            int_left, int_top, filename=outfile, do_round=False
        )
        row_bottom, col_right = io.xy_to_rowcol(int_right, int_bottom, filename=outfile)
        # Read it in so we can blend out the write for this block
        cur_out = io.load_gdal(
            outfile, rows=slice(row_top, row_bottom), cols=slice(col_left, col_right)
        )
        in_nodata = io.get_nodata(f)
        cur_out = _blend_new_arr(
            cur_out, arr_in, nodata_vals=[in_nodata, out_nodata, np.nan]
        )
        # Write the input data to the output in this window
        io.write_block(
            cur_out,
            filename=outfile,
            row_start=row_top,
            col_start=col_left,
        )

    # Remove the tempdir
    temp_dir.cleanup()


def _blend_new_arr(
    cur_arr: np.ndarray, new_arr: np.ndarray, nodata_vals: List[Optional[float]]
):
    """Blend two arrays together, replacing values in cur_arr with new_arr.

    Currently, the only blending method is to overwrite `cur_arr` with `new_arr` where
    `new_arr` has data.

    Parameters
    ----------
    cur_arr : np.ndarray
        The array to blend into.
    new_arr : np.ndarray
        The new array to add/overwrite with.
    nodata_vals : List[float]
        The nodata values to replace in cur_arr.

    Returns
    -------
    np.ndarray
        The blended array.
    """
    # Replace nodata values in cur_arr with new_arr
    good_pixels = np.ones(cur_arr.shape, dtype=bool)
    for nodata in nodata_vals:
        if nodata is not None:
            if np.isnan(nodata):
                nd_mask = np.isnan(new_arr)
            else:
                nd_mask = new_arr == nodata
            good_pixels = good_pixels & ~nd_mask

    # Replace the values in cur_arr with new_arr, where new_arr is not nodata
    cur_arr[good_pixels] = new_arr[good_pixels]
    return cur_arr


def _warp_to_projection(
    filenames: List[Filename],
    dirname: Filename,
    projection: str,
    res: Tuple[float, float],
) -> List[Path]:
    """Warp a list of files to the most common projection.

    Parameters
    ----------
    filenames : List[Filename]
        List of filenames to warp.
    dirname : Filename
        The directory to write the warped files to.
    projection : str
        The desired projection, as a WKT string or 'EPSG:XXXX' string.
    res : Tuple[float, float]
        The desired [x, y] resolution.

    Returns
    -------
    List[Filename]
        The warped filenames.
    """
    warped_files = []
    for fn in filenames:
        fn = Path(fn)
        ds = gdal.Open(fspath(fn))
        proj_in = ds.GetProjection()
        if proj_in == projection:
            warped_files.append(fn)
            continue
        warped_fn = Path(dirname) / f"{fn.name}.warped.tif"
        from_srs_name = ds.GetSpatialRef().GetName()
        to_srs_name = osr.SpatialReference(projection).GetName()
        logger.info(
            f"Reprojecting {fn} from {from_srs_name} to match mode projection"
            f" {to_srs_name}"
        )
        warped_files.append(warped_fn)
        gdal.Warp(
            fspath(warped_fn),
            fspath(fn),
            format="VRT",  # Just creates a file that will warp on the fly
            dstSRS=projection,
            resampleAlg="lanczos",  # sinc-kernel for resampling
            targetAlignedPixels=True,  # align in multiples of dx, dy
            xRes=res[0],
            yRes=res[1],
        )

    return warped_files


def _get_mode_projection(filenames: List[Filename]) -> str:
    """Get the most common projection in the list."""
    projs = [gdal.Open(fspath(fn)).GetProjection() for fn in filenames]
    return max(set(projs), key=projs.count)


def _get_resolution(filenames: List[Filename]) -> Tuple[float, float]:
    """Get the most common resolution in the list."""
    gts = [gdal.Open(fspath(fn)).GetGeoTransform() for fn in filenames]
    res = [(gt[1], gt[5]) for gt in gts]
    if len(set(res)) > 1:
        raise ValueError(f"The input files have different resolutions: {res}. ")
    return res[0]


def _get_combined_bounds_gt(
    *filenames: Filename,
    target_aligned_pixels: bool = False,
) -> Tuple[Tuple[float, float, float, float], List]:
    """Get the bounds and geotransform of the combined image.

    Parameters
    ----------
    filenames : List[Filename]
        list of filenames to combine
    target_aligned_pixels : bool
        if True, adjust output image bounds so that pixel coordinates
        are integer multiples of pixel size, matching the ``-tap``.

    Returns
    -------
    bounds : Tuple[float]
        (min_x, min_y, max_x, max_y)
    gt : List[float]
        geotransform of the combined image.
    """
    # scan input files
    xs = []
    ys = []
    resolutions = set()
    projs = set()
    for fn in filenames:
        ds = gdal.Open(fspath(fn))
        left, bottom, right, top = io.get_raster_bounds(fn)
        gt = ds.GetGeoTransform()
        dx, dy = gt[1], gt[5]

        resolutions.add((abs(dx), abs(dy)))  # dy is negative for north-up
        projs.add(ds.GetProjection())

        xs.extend([left, right])
        ys.extend([bottom, top])

    if len(resolutions) > 1:
        raise ValueError(f"The input files have different resolutions: {resolutions}. ")
    if len(projs) > 1:
        raise ValueError(f"The input files have different projections: {projs}. ")

    res = (abs(dx), abs(dy))
    bounds = min(xs), min(ys), max(xs), max(ys)
    if target_aligned_pixels:
        bounds = _align_bounds(bounds, res)

    gt_total = [bounds[0], dx, 0, bounds[3], 0, dy]
    return bounds, gt_total


def _get_output_shape(bounds, res):
    """Get the output shape of the combined image."""
    left, bottom, right, top = bounds
    out_width = int(round((right - left) / abs(res[0])))
    out_height = int(round((top - bottom) / abs(res[1])))
    return (out_height, out_width)


def _align_bounds(bounds, res):
    left, bottom, right, top = bounds
    left = math.floor(left / res[0]) * res[0]
    right = math.ceil(right / res[0]) * res[0]
    bottom = math.floor(bottom / res[1]) * res[1]
    top = math.ceil(top / res[1]) * res[1]
    return (left, bottom, right, top)


def _nodata_to_zero(
    infile,
    outfile: Optional[Filename] = None,
    ext: Optional[str] = None,
    in_band: int = 1,
    driver="ENVI",
    creation_options=["SUFFIX=ADD"],
):
    """Make a copy of infile and replace NaNs with 0."""
    in_p = Path(infile)
    if outfile is None:
        if ext is None:
            ext = in_p.suffix
        out_dir: Path = in_p.parent if out_dir is None else Path(out_dir)
        outfile = out_dir / (in_p.stem + "_tmp" + ext)

    ds_in = gdal.Open(fspath(infile))
    drv = gdal.GetDriverByName(driver)
    ds_out = drv.CreateCopy(fspath(outfile), ds_in, options=creation_options)

    bnd = ds_in.GetRasterBand(in_band)
    nodata = bnd.GetNoDataValue()
    arr = bnd.ReadAsArray()
    # also make sure to replace NaNs, even if nodata is not set
    mask = np.logical_or(np.isnan(arr), arr == nodata)
    arr[mask] = 0

    ds_out.GetRasterBand(1).WriteArray(arr)
    ds_out = None

    return outfile
