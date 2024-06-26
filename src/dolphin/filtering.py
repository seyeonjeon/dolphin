import numpy as np
from numpy.typing import ArrayLike
from scipy import ndimage


def filtering(
    unw_ifg: ArrayLike,
    corr: ArrayLike,
    mask_cutoff: float = 0.5,
    wavelength_cutoff: float = 50 * 1e3,
    dx: float = 30,
) -> np.ndarray:
    """Filter out signals with spatial wavelength longer than a threshold.

    Parameters
    ----------
    unw_ifg : np.ndarray, 2D complex array
        unwrapped interferogram to interpolate
    corr : 2D float array
        Array of interferometric correlation from 0 to 1.
    mask_cutoff: float
        Threshold to use on `corr` so that pixels where
        `corr[i, j] > mask_cutoff` are True and the rest are False.
        The default is 0.5.
    wavelength_cutoff: float
        Spatial wavelength threshold to filter unw_ifg.
        Signals with wavelength longer than 'wavelength_curoff' in unw_ifg will be filtered out.
        The default is 50*1e3 (m).
    dx : float
        Pixel spatial spacing. Assume same spacing for x, y axes.
        The default is 30 (m).


    Returns
    -------
    filtered_ifg : 2D complex array
        filtered interferogram that does not contain signals with spatial wavelength longer than a threshold.

    """
    nrow, ncol = corr.shape

    # Create Boolean mask for corr > mask_cutoff to be True and the rest to be False
    mask = (corr > mask_cutoff).astype("bool")

    # Create Boolean mask for Zero-filled boundary area to be False and the rest to be True
    mask_boundary = ~(corr == 0).astype("bool")

    # Ramp plane fitting
    Y = unw_ifg[mask]  # get data of non NaN & masked pixels
    Xdata = np.argwhere(mask)  # get indices of non NaN & masked pixels
    X = np.c_[np.ones((len(Xdata))), Xdata]
    theta = np.dot(np.dot(np.linalg.pinv(np.dot(X.transpose(), X)), X.transpose()), Y)
    X1_, X2_ = np.mgrid[:nrow, :ncol]
    X_ = np.hstack(
        (np.reshape(X1_, (nrow * ncol, 1)), np.reshape(X2_, (nrow * ncol, 1)))
    )
    X_ = np.hstack((np.ones((nrow * ncol, 1)), X_))
    plane = np.reshape(np.dot(X_, theta), (nrow, ncol))

    # Replace masked out pixels with the ramp plane
    unw_ifg_interp = np.copy(unw_ifg)
    unw_ifg_interp[~mask * mask_boundary] = plane[~mask * mask_boundary]

    # Copy the edge pixels for the boundary area before filling them by reflection
    EV_fill = np.copy(unw_ifg_interp)

    NW = Xdata[np.argmin(Xdata[:, 0])]  # Get indices of upper left corner pixel
    SE = Xdata[np.argmax(Xdata[:, 0])]  # Get indices of lower right corner pixel
    SW = Xdata[np.argmin(Xdata[:, 1])]  # Get indices of lower left corner pixel
    NE = Xdata[np.argmax(Xdata[:, 1])]  # Get indices of upper left corner pixel

    for k in range(NW[1], NE[1] + 1):
        n_zeros = np.count_nonzero(
            unw_ifg_interp[0 : NE[0] + 1, k] == 0
        )  # count zeros in North direction
        EV_fill[0:n_zeros, k] = EV_fill[n_zeros, k]
    for k in range(SW[1], SE[1] + 1):
        n_zeros = np.count_nonzero(
            unw_ifg_interp[SW[0] + 1 :, k] == 0
        )  # count zeros in South direction
        EV_fill[-n_zeros:, k] = EV_fill[-n_zeros - 1, k]
    for k in range(NW[0], SW[0] + 1):
        n_zeros = np.count_nonzero(
            unw_ifg_interp[k, 0 : NW[1] + 1] == 0
        )  # count zeros in North direction
        EV_fill[k, 0:n_zeros] = EV_fill[k, n_zeros]
    for k in range(NE[0], SE[0] + 1):
        n_zeros = np.count_nonzero(
            unw_ifg_interp[k, SE[1] + 1 :] == 0
        )  # count zeros in North direction
        EV_fill[k, -n_zeros:] = EV_fill[k, -n_zeros - 1]

    # Fill the boundary area reflecting the pixel values
    Reflect_fill = np.copy(EV_fill)

    for k in range(NW[1], NE[1] + 1):
        n_zeros = np.count_nonzero(
            unw_ifg_interp[0 : NE[0] + 1, k] == 0
        )  # count zeros in North direction
        Reflect_fill[0:n_zeros, k] = np.flipud(EV_fill[n_zeros : n_zeros + n_zeros, k])
    for k in range(SW[1], SE[1] + 1):
        n_zeros = np.count_nonzero(
            unw_ifg_interp[SW[0] + 1 :, k] == 0
        )  # count zeros in South direction
        Reflect_fill[-n_zeros:, k] = np.flipud(
            EV_fill[-n_zeros - n_zeros : -n_zeros, k]
        )
    for k in range(NW[0], SW[0] + 1):
        n_zeros = np.count_nonzero(
            unw_ifg_interp[k, 0 : NW[1] + 1] == 0
        )  # count zeros in North direction
        Reflect_fill[k, 0:n_zeros] = np.flipud(EV_fill[k, n_zeros : n_zeros + n_zeros])
    for k in range(NE[0], SE[0] + 1):
        n_zeros = np.count_nonzero(
            unw_ifg_interp[k, SE[1] + 1 :] == 0
        )  # count zeros in North direction
        Reflect_fill[k, -n_zeros:] = np.flipud(
            EV_fill[k, -n_zeros - n_zeros : -n_zeros]
        )

    Reflect_fill[0 : NW[0], 0 : NW[1]] = np.flipud(
        Reflect_fill[NW[0] : NW[0] + NW[0], 0 : NW[1]]
    )  # upper left corner area
    Reflect_fill[0 : NE[0], NE[1] + 1 :] = np.fliplr(
        Reflect_fill[0 : NE[0], NE[1] + 1 - (ncol - NE[1] - 1) : NE[1] + 1]
    )  # upper right corner area
    Reflect_fill[SW[0] + 1 :, 0 : SW[1]] = np.fliplr(
        Reflect_fill[SW[0] + 1 :, SW[1] : SW[1] + SW[1]]
    )  # lower left corner area
    Reflect_fill[SE[0] + 1 :, SE[1] + 1 :] = np.flipud(
        Reflect_fill[SE[0] + 1 - (nrow - SE[0] - 1) : SE[0] + 1, SE[1] + 1 :]
    )  # lower right corner area

    # 2D filtering with Gaussian kernel
    # wavelength_cutoff: float = 50*1e3,
    # dx: float = 30,
    cutoff_value = 0.5  # 0 < cutoff_value < 1
    sigma_f = (
        1 / wavelength_cutoff / np.sqrt(np.log(1 / cutoff_value))
    )  # fc = sqrt(ln(1/cutoff_value))*sigma_f
    sigma_x = 1 / np.pi / 2 / sigma_f
    sigma = sigma_x / dx

    lowpass_filtered = ndimage.gaussian_filter(Reflect_fill, sigma)
    filtered_ifg = unw_ifg - lowpass_filtered * mask_boundary

    return filtered_ifg
