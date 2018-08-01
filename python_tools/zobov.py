from __future__ import print_function
import sys
import os
import numpy as np
import healpy as hp
import random
import imp
import subprocess
import glob
from scipy.spatial import cKDTree
from scipy.integrate import quad
from cosmology import Cosmology
from astropy.io import fits
from scipy.signal import savgol_filter
from scipy.interpolate import InterpolatedUnivariateSpline


class ZobovVoids:

    def __init__(self, do_tessellation=True, tracer_file="", handle="", output_folder="", is_box=True, boss_like=False,
                 special_patchy=False, posn_cols=np.array([0, 1, 2]), box_length=2500.0, omega_m=0.308, mask_file="",
                 use_z_wts=True, use_ang_wts=True, z_min=0.43, z_max=0.7, mock_file="", mock_dens_ratio=10,
                 min_dens_cut=1.0, void_min_num=1, use_barycentres=True, void_prefix="", find_clusters=False,
                 max_dens_cut=1.0, cluster_min_num=1, cluster_prefix=""):

        print(" ==== Starting the void-finding with ZOBOV ==== ")

        # the prefix/handle used for all output file names
        self.handle = handle

        # output folder
        self.output_folder = output_folder

        # file path for ZOBOV-formatted tracer data
        self.posn_file = output_folder + handle + "_pos.dat"

        # input file
        self.tracer_file = tracer_file
        # check input file exists ...
        if not os.access(tracer_file, os.F_OK):
            sys.exit("Can't find tracer file %s, aborting" % tracer_file)

        # (Boolean) choice between cubic simulation box and sky survey
        self.is_box = is_box

        # load tracer information
        print("Loading tracer positions from file %s" % tracer_file)
        if boss_like:  # FITS format input file
            if self.is_box:
                print('Both boss_like and is_box cannot be simultaneously True! Setting is_box = False')
                self.is_box = False
            input_data = fits.open(self.tracer_file)[1].data
            ra = input_data.RA
            dec = input_data.DEC
            redshift = input_data.Z
            self.num_tracers = len(ra)
            tracers = np.empty((self.num_tracers, 3))
            tracers[:, 0] = ra
            tracers[:, 1] = dec
            tracers[:, 2] = redshift
        else:
            if '.npy' in tracer_file:
                tracers = np.load(tracer_file)
            else:
                tracers = np.loadtxt(tracer_file)
            # test that tracer information is valid
            if not tracers.shape[1] >= 3:
                sys.exit("Not enough columns, need 3D position information. Aborting")
            if not len(posn_cols) == 3:
                sys.exit("You must specify 3 columns containing tracer position information. Aborting")

            if special_patchy:
                # if the special PATCHY mock format is used and the vetomask has not already been applied
                # during reconstruction, apply it now
                veto_cut = tracers[:, 6] == 1
                tracers = tracers[veto_cut, :]
            self.num_tracers = tracers.shape[0]
            print("%d tracers found" % self.num_tracers)

            # keep only the tracer position information
            tracers = tracers[:, posn_cols]

        # now complete the rest of the data preparation
        if self.is_box:  # dealing with cubic simulation box
            if box_length <= 0:
                sys.exit("Zero or negative box length, aborting")
            self.box_length = box_length

            # check that tracer positions lie within the box, wrap using PBC if not
            tracers[tracers[:, 0] > box_length, 0] -= box_length
            tracers[tracers[:, 1] > box_length, 1] -= box_length
            tracers[tracers[:, 2] > box_length, 2] -= box_length
            tracers[tracers[:, 0] < 0, 0] += box_length
            tracers[tracers[:, 1] < 0, 1] += box_length
            tracers[tracers[:, 2] < 0, 2] += box_length

            # determine mean tracer number density
            self.tracer_dens = 1.0 * self.num_tracers / (box_length ** 3.)

            self.num_mocks = 0
            self.num_part_total = self.num_tracers
            self.tracers = tracers
        else:
            # set cosmology
            self.omega_m = omega_m
            cosmo = Cosmology(omega_m=omega_m)
            self.cosmo = cosmo

            # convert input tracer information to standard format
            self.coords_radecz2std(tracers[:, 0], tracers[:, 1], tracers[:, 2])

            self.z_min = z_min
            self.z_max = z_max

            # check and cut on the provided redshift limits
            if np.min(self.tracers[:, 5]) < self.z_min or np.max(self.tracers[:, 5]) > self.z_max:
                print('Cutting galaxies outside the redshift limits')
                zselect = (self.z_min < self.tracers[:, 5]) & (self.tracers[:, 5] < self.z_max)
                self.tracers = self.tracers[zselect, :]

            # sky mask file (should be in Healpy FITS format)
            if not os.access(mask_file, os.F_OK):
                print("Sky mask not provided or not found, generating approximate one")
                self.mask_file = self.output_folder + self.handle + '_mask.fits'
                self.f_sky = self.generate_mask()
            else:
                mask = hp.read_map(mask_file, verbose=False)
                self.mask_file = mask_file
                # check whether the mask is correct
                ra = self.tracers[:, 3]
                dec = self.tracers[:, 4]
                nside = hp.get_nside(mask)
                pixels = hp.ang2pix(nside, np.deg2rad(90 - dec), np.deg2rad(ra))
                if np.any(mask[pixels] == 0):
                    print('Galaxies exist where mask=0. Removing these to avoid errors later.')
                    all_indices = np.arange(len(self.tracers))
                    bad_inds = np.where(mask[pixels] == 0)[0]
                    good_inds = all_indices[np.logical_not(np.in1d(all_indices, bad_inds))]
                    self.tracers = self.tracers[good_inds, :]

                # effective sky fraction
                self.f_sky = 1.0 * np.sum(mask) / len(mask)

            # finally, remove any instances of two galaxies at the same location, otherwise tessellation will fail
            # (this is a problem with PATCHY mocks, not seen any such instances in real survey data ...)
            # NOTE: the following line will not work with older versions of numpy!!
            unique_tracers = np.unique(self.tracers, axis=0)
            if unique_tracers.shape[0] < self.tracers.shape[0]:
                print(
                    'Removing %d galaxies with duplicate positions' % (self.tracers.shape[0] - unique_tracers.shape[0]))
            self.tracers = unique_tracers

            # update galaxy stats
            self.num_tracers = self.tracers.shape[0]
            print('Kept %d tracers after all cuts' % self.num_tracers)

            # calculate mean density
            self.r_near = self.cosmo.get_comoving_distance(self.z_min)
            self.r_far = self.cosmo.get_comoving_distance(self.z_max)
            survey_volume = self.f_sky * 4 * np.pi * (self.r_far ** 3. - self.r_near ** 3.) / 3.
            self.tracer_dens = self.num_tracers / survey_volume

            # weights options: correct for z-dependent selection and angular completeness
            self.use_z_wts = use_z_wts
            if use_z_wts:
                self.selection_fn_file = self.output_folder + self.handle + '_selFn.txt'
                self.generate_selfn(nbins=15)
            self.use_ang_wts = use_ang_wts

            if do_tessellation:
                # options for buffer mocks around survey boundaries
                if mock_file == '':
                    # no buffer mocks provided, so generate new
                    print('Generating buffer mocks around survey edges ...')
                    print('\tbuffer mocks will have %0.1f x the galaxy number density' % mock_dens_ratio)
                    self.mock_dens_ratio = mock_dens_ratio
                    self.generate_buffer()
                elif not os.access(mock_file, os.F_OK):
                    print('Could not find file %s containing buffer mocks!' % mock_file)
                    print('Generating buffer mocks around survey edges ...')
                    print('\tbuffer mocks will have %0.1f x the galaxy number density' % mock_dens_ratio)
                    self.mock_dens_ratio = mock_dens_ratio
                    self.generate_buffer()
                else:
                    print('Loading pre-computed buffer mocks from file %s' % mock_file)
                    if '.npy' in mock_file:
                        buffers = np.load(mock_file)
                    else:
                        buffers = np.loadtxt(mock_file)
                    # recalculate the box length
                    self.box_length = 2.0 * np.max(np.abs(buffers[:, :3])) + 1
                    self.num_mocks = buffers.shape[0]
                    # join the buffers to the galaxy tracers
                    self.tracers = np.vstack([self.tracers, buffers])
                    self.num_part_total = self.num_tracers + self.num_mocks
                    self.mock_file = mock_file
                # shift Cartesian positions from observer to box coordinates
                self.tracers[:, :3] += 0.5 * self.box_length

        # for easy debugging: write all tracer positions to file
        # np.save(self.posn_file.replace('pos.dat', 'pos.npy'), self.tracers)

        self.num_non_edge = self.num_tracers

        # options for void-finding
        self.min_dens_cut = min_dens_cut
        self.void_min_num = void_min_num
        self.use_barycentres = use_barycentres

        # prefix for naming void files
        self.void_prefix = void_prefix

        # options for finding 'superclusters'
        self.find_clusters = find_clusters
        if find_clusters:
            self.cluster_min_num = cluster_min_num
            self.max_dens_cut = max_dens_cut
            self.cluster_prefix = cluster_prefix

    def coords_radecz2std(self, ra, dec, redshift):
        """Converts sky coordinates in (RA,Dec,redshift) to standard form, including comoving
        Cartesian coordinate information
        """

        # convert galaxy redshifts to comoving distances
        rdist = self.cosmo.get_comoving_distance(redshift)

        # convert RA, Dec angles in degrees to theta, phi in radians
        phi = ra * np.pi / 180.
        theta = np.pi / 2. - dec * np.pi / 180.

        # obtain Cartesian coordinates
        galaxies = np.zeros((self.num_tracers, 6))
        galaxies[:, 0] = rdist * np.sin(theta) * np.cos(phi)  # r*cos(ra)*cos(dec)
        galaxies[:, 1] = rdist * np.sin(theta) * np.sin(phi)  # r*sin(ra)*cos(dec)
        galaxies[:, 2] = rdist * np.cos(theta)  # r*sin(dec)
        # standard format includes RA, Dec, redshift info
        galaxies[:, 3] = ra
        galaxies[:, 4] = dec
        galaxies[:, 5] = redshift

        self.tracers = galaxies

    def generate_mask(self):
        """Generates an approximate survey sky mask if none is provided, and saves to file

        Returns: f_sky
        """

        nside = 64
        npix = hp.nside2npix(nside)

        # use tracer RA,Dec info to see which sky pixels are occupied
        phi = self.tracers[:, 3] * np.pi / 180.
        theta = np.pi / 2. - self.tracers[:, 4] * np.pi / 180.
        pixels = hp.ang2pix(nside, theta, phi)

        # very crude binary mask
        mask = np.zeros(npix)
        mask[pixels] = 1.

        # write this mask to file
        hp.write_map(self.mask_file, mask)

        # return sky fraction
        f_sky = 1.0 * sum(mask) / len(mask)
        return f_sky

    def find_mask_boundary(self, completeness_limit=0.):
        """Finds pixels adjacent to but outside the survey mask

        Arguments:
            completeness_limit: value in range (0,1), sets completeness lower limit for boundary determination

        Returns:
            boundary: a binary Healpix map with the survey mask boundary"""

        mask = hp.read_map(self.mask_file, verbose=False)
        mask = hp.ud_grade(mask, 512)
        nside = hp.get_nside(mask)
        npix = hp.nside2npix(nside)
        boundary = np.zeros(npix)

        # find pixels outside the mask that neighbour pixels within it
        # do this step in a loop, to get a thicker boundary layer
        for j in range(2 + nside / 128):
            if j == 0:
                filled_inds = np.nonzero(mask > completeness_limit)[0]
            else:
                filled_inds = np.nonzero(boundary)[0]
            theta, phi = hp.pix2ang(nside, filled_inds)
            neigh_pix = hp.get_all_neighbours(nside, theta, phi)
            for i in range(neigh_pix.shape[1]):
                outsiders = neigh_pix[(mask[neigh_pix[:, i]] <= completeness_limit) & (neigh_pix[:, i] > -1)
                                      & (boundary[neigh_pix[:, i]] == 0), i]
                # >-1 condition takes care of special case where neighbour wasn't found
                if j == 0:
                    boundary[outsiders] = 2
                else:
                    boundary[outsiders] = 1
        boundary[boundary == 2] = 0

        if nside <= 128:
            # upgrade the boundary to aid placement of buffer mocks
            boundary = hp.ud_grade(boundary, 128)

        return boundary

    def generate_buffer(self):
        """Method to generate buffer particles around the edges of survey volume to prevent and detect leakage of
        Voronoi cells outside survey region during the tessellation stage"""

        # set the buffer particle density
        buffer_dens = self.mock_dens_ratio * self.tracer_dens

        # get the survey mask
        mask = hp.read_map(self.mask_file, verbose=False)
        nside = hp.get_nside(mask)
        survey_pix = np.nonzero(mask)[0]
        numpix = len(survey_pix)

        # estimate the mean inter-particle separation
        mean_nn_distance = self.tracer_dens ** (-1. / 3)

        # ---- Step 1: buffer particles along the high-redshift cap---- #
        # get the maximum redshift of the survey galaxies
        z_high = np.max([np.max(self.tracers[:, 5]), self.z_max])

        # define the radial extents of the layer in which we will place the buffer particles
        # these choices are somewhat arbitrary, and could be optimized
        r_low = self.cosmo.get_comoving_distance(z_high) + mean_nn_distance * self.mock_dens_ratio ** (-1. / 3)
        r_high = r_low + mean_nn_distance
        cap_volume = self.f_sky * 4. * np.pi * (r_high ** 3. - r_low ** 3.) / 3.

        # how many buffer particles fit in this cap
        num_high_mocks = int(np.ceil(buffer_dens * cap_volume))
        high_mocks = np.zeros((num_high_mocks, 6))

        # generate random radial positions within the cap
        rdist = (r_low ** 3. + (r_high ** 3. - r_low ** 3.) * np.random.rand(num_high_mocks)) ** (1. / 3)

        # generate mock angular positions within the survey mask
        # NOTE: these are not random positions, since they are all centred in a Healpix pixel
        # but for buffer particles this is not important (and is generally faster)
        while num_high_mocks > numpix:
            # more mock posns required than mask pixels, so upgrade mask to get more pixels
            nside *= 2
            mask = hp.ud_grade(mask, nside)
            survey_pix = np.nonzero(mask)[0]
            numpix = len(survey_pix)
        rand_pix = survey_pix[random.sample(np.arange(numpix), num_high_mocks)]
        theta, phi = hp.pix2ang(nside, rand_pix)

        # convert to standard format
        high_mocks[:, 0] = rdist * np.sin(theta) * np.cos(phi)
        high_mocks[:, 1] = rdist * np.sin(theta) * np.sin(phi)
        high_mocks[:, 2] = rdist * np.cos(theta)
        high_mocks[:, 3] = phi * 180. / np.pi
        high_mocks[:, 4] = 90 - theta * 180. / np.pi
        high_mocks[:, 5] = -1  # all buffer particles are given redshift -1 to aid identification

        # farthest buffer particle
        self.r_far = np.max(rdist)

        print("\tplaced %d buffer mocks at high-redshift cap" % num_high_mocks)

        buffers = high_mocks
        self.num_mocks = num_high_mocks
        # ------------------------------------------------------------- #

        # ----- Step 2: buffer particles along the low-redshift cap---- #
        z_low = np.min([np.min(self.tracers[:, 5]), self.z_min])
        if z_low > 0:
            # define the radial extents of the layer in which we will place the buffer particles
            # these choices are somewhat arbitrary, and could be optimized
            r_high = self.cosmo.get_comoving_distance(z_low) - mean_nn_distance * self.mock_dens_ratio ** (-1. / 3)
            r_low = r_high - mean_nn_distance
            if r_high < 0:
                r_high = self.cosmo.get_comoving_distance(z_low)
            if r_low < 0:
                r_low = 0
            cap_volume = self.f_sky * 4. * np.pi * (r_high ** 3. - r_low ** 3.) / 3.

            # how many buffer particles fit in this cap
            num_low_mocks = int(np.ceil(buffer_dens * cap_volume))
            low_mocks = np.zeros((num_low_mocks, 6))

            # generate random radial positions within the cap
            rdist = (r_low ** 3. + (r_high ** 3. - r_low ** 3.) * np.random.rand(num_low_mocks)) ** (1. / 3)

            # generate mock angular positions within the survey mask
            # same as above -- these are not truly random but that's ok
            while num_low_mocks > numpix:
                # more mock posns required than mask pixels, so upgrade mask to get more pixels
                nside *= 2
                mask = hp.ud_grade(mask, nside)
                survey_pix = np.nonzero(mask)[0]
                numpix = len(survey_pix)
            rand_pix = survey_pix[random.sample(np.arange(numpix), num_low_mocks)]
            theta, phi = hp.pix2ang(nside, rand_pix)

            # convert to standard format
            low_mocks[:, 0] = rdist * np.sin(theta) * np.cos(phi)
            low_mocks[:, 1] = rdist * np.sin(theta) * np.sin(phi)
            low_mocks[:, 2] = rdist * np.cos(theta)
            low_mocks[:, 3] = phi * 180. / np.pi
            low_mocks[:, 4] = 90 - theta * 180. / np.pi
            low_mocks[:, 5] = -1.  # all buffer particles are given redshift -1 to aid later identification

            # closest buffer particle
            self.r_near = np.min(rdist)

            print("\tplaced %d buffer mocks at low-redshift cap" % num_low_mocks)

            buffers = np.vstack([buffers, low_mocks])
            self.num_mocks += num_low_mocks
        else:
            print("\tno buffer mocks required at low-redshift cap")
        # ------------------------------------------------------------- #

        # ------ Step 3: buffer particles along the survey edges-------- #
        if self.f_sky < 1.0:
            # get the survey boundary
            boundary = self.find_mask_boundary(completeness_limit=0.0)

            # where we will place the buffer mocks
            boundary_pix = np.nonzero(boundary)[0]
            numpix = len(boundary_pix)
            boundary_f_sky = 1.0 * len(boundary_pix) / len(boundary)
            boundary_nside = hp.get_nside(boundary)

            # how many buffer particles
            # boundary_volume = boundary_f_sky * 4. * np.pi * (self.r_far ** 3. - self.r_near ** 3.) / 3.
            boundary_volume = boundary_f_sky * 4. * np.pi * quad(lambda y: y ** 2, self.r_near, self.r_far)[0]
            num_bound_mocks = int(np.ceil(buffer_dens * boundary_volume))
            bound_mocks = np.zeros((num_bound_mocks, 6))

            # generate random radial positions within the boundary layer
            rdist = (self.r_near ** 3. + (self.r_far ** 3. - self.r_near ** 3.) *
                     np.random.rand(num_bound_mocks)) ** (1. / 3)

            # generate mock angular positions within the boundary layer
            # and same as above -- not truly random, but ok
            while num_bound_mocks > numpix:
                # more mocks required than pixels in which to place them, so upgrade mask
                boundary_nside *= 2
                boundary = hp.ud_grade(boundary, boundary_nside)
                boundary_pix = np.nonzero(boundary)[0]
                numpix = len(boundary_pix)
            rand_pix = boundary_pix[random.sample(np.arange(numpix), num_bound_mocks)]
            theta, phi = hp.pix2ang(boundary_nside, rand_pix)

            # convert to standard format
            bound_mocks[:, 0] = rdist * np.sin(theta) * np.cos(phi)
            bound_mocks[:, 1] = rdist * np.sin(theta) * np.sin(phi)
            bound_mocks[:, 2] = rdist * np.cos(theta)
            bound_mocks[:, 3] = phi * 180. / np.pi
            bound_mocks[:, 4] = 90 - theta * 180. / np.pi
            bound_mocks[:, 5] = -1.  # all buffer particles are given redshift -1 to aid identification

            print("\tplaced %d buffer mocks along the survey boundary edges" % num_bound_mocks)

            buffers = np.vstack([buffers, bound_mocks])
            self.num_mocks += num_bound_mocks
        else:
            print("\tdata covers the full sky, no buffer mocks required along edges")
        # ------------------------------------------------------------- #

        # determine the size of the cubic box required
        self.box_length = 2.0 * np.max(np.abs(buffers[:, :3])) + 1.
        print("\tUsing box length %0.2f" % self.box_length)

        # ------ Step 4: guard buffers to stabilize the tessellation-------- #
        # (strictly speaking, this gives a lot of redundancy as the box is very big;
        # but it doesn't slow the tessellation too much and keeps coding simpler)

        # generate guard particle positions
        x = np.linspace(0.1, self.box_length - 0.1, 20)
        guards = np.vstack(np.meshgrid(x, x, x)).reshape(3, -1).T

        # make a kdTree instance using all the galaxies and buffer mocks
        all_positions = np.vstack([self.tracers[:, :3], buffers[:, :3]])
        all_positions += self.box_length / 2.  # from observer to box coordinates
        tree = cKDTree(all_positions, boxsize=self.box_length)

        # find the nearest neighbour distance for each of the guard particles
        nn_dist = np.empty(len(guards))
        for i in range(len(guards)):
            nn_dist[i], nnind = tree.query(guards[i, :], k=1)

        # drop all guards that are too close to existing points
        guards = guards[nn_dist > (self.box_length - 0.2) / 20.]
        guards = guards - self.box_length / 2.  # guard positions back in observer coordinates

        # convert to standard format
        num_guard_mocks = len(guards)
        guard_mocks = np.zeros((num_guard_mocks, 6))
        guard_mocks[:, :3] = guards
        guard_mocks[:, 3:5] = -60.  # guards are given RA and Dec -60 as well to distinguish them from other buffers
        guard_mocks[:, 5] = -1.

        print("\tadded %d guards to stabilize the tessellation" % num_guard_mocks)

        buffers = np.vstack([buffers, guard_mocks])
        self.num_mocks += num_guard_mocks
        # ------------------------------------------------------------------ #

        # write the buffer information to file for later reference
        mock_file = self.posn_file.replace('pos.dat', 'mocks.npy')
        print('Buffer mocks written to file %s' % mock_file)
        np.save(mock_file, buffers)
        self.mock_file = mock_file

        # now add buffer particles to tracers
        self.tracers = np.vstack([self.tracers, buffers])

        self.num_part_total = self.num_tracers + self.num_mocks

    def generate_selfn(self, nbins=20):
        """Measures the redshift-dependence of the galaxy number density in equal-volume redshift bins,
         and writes the selection function to file.

        Arguments:
          nbins: number of bins to use
        """

        print('Determining survey redshift selection function ...')

        # first determine the equal volume bins
        r_near = self.cosmo.get_comoving_distance(self.z_min)
        r_far = self.cosmo.get_comoving_distance(self.z_max)
        rvals = np.linspace(r_near ** 3, r_far ** 3, nbins + 1)
        rvals = rvals ** (1. / 3)
        zsteps = self.cosmo.get_redshift(rvals)
        volumes = self.f_sky * 4 * np.pi * (rvals[1:] ** 3. - rvals[:-1] ** 3.) / 3.
        # (all elements of volumes should be equal)

        # get the tracer galaxy redshifts
        redshifts = self.tracers[:, 5]

        # histogram and calculate number density
        hist, zsteps = np.histogram(redshifts, bins=zsteps)
        nofz = hist / volumes
        zmeans = np.zeros(len(hist))
        for i in range(len(hist)):
            zmeans[i] = np.mean(redshifts[np.logical_and(redshifts >= zsteps[i], redshifts < zsteps[i + 1])])

        output = np.zeros((len(zmeans), 3))
        output[:, 0] = zmeans
        output[:, 1] = nofz
        output[:, 2] = nofz / self.tracer_dens

        # write to file
        np.savetxt(self.selection_fn_file, output, fmt='%0.3f %0.4e %0.4f', header='z n(z) f(z)')

    def write_box_zobov(self):
        """Writes the tracer and mock position information to file in a ZOBOV-readable format"""

        with open(self.posn_file, 'w') as F:
            npart = np.array(self.num_part_total, dtype=np.int32)
            npart.tofile(F, format='%d')
            data = self.tracers[:, 0]
            data.tofile(F, format='%f')
            data = self.tracers[:, 1]
            data.tofile(F, format='%f')
            data = self.tracers[:, 2]
            data.tofile(F, format='%f')
            if not self.is_box:  # write RA, Dec and redshift too
                data = self.tracers[:, 3]
                data.tofile(F, format='%f')
                data = self.tracers[:, 4]
                data.tofile(F, format='%f')
                data = self.tracers[:, 5]
                data.tofile(F, format='%f')

    def delete_tracer_info(self):
        """removes the tracer information if no longer required, to save memory"""

        self.tracers = 0

    def reread_tracer_info(self):
        """re-reads tracer information from file if required after previous deletion"""

        self.tracers = np.empty((self.num_part_total, 6))
        with open(self.posn_file, 'r') as F:
            nparts = np.fromfile(F, dtype=np.int32, count=1)[0]
            if not nparts == self.num_part_total:  # sanity check
                sys.exit("nparts = %d in %s_pos.dat file does not match num_part_total = %d!"
                         % (nparts, self.handle, self.num_part_total))
            self.tracers[:, 0] = np.fromfile(F, dtype=np.float64, count=nparts)
            self.tracers[:, 1] = np.fromfile(F, dtype=np.float64, count=nparts)
            self.tracers[:, 2] = np.fromfile(F, dtype=np.float64, count=nparts)
            if not self.is_box:
                self.tracers[:, 3] = np.fromfile(F, dtype=np.float64, count=nparts)
                self.tracers[:, 4] = np.fromfile(F, dtype=np.float64, count=nparts)
                self.tracers[:, 5] = np.fromfile(F, dtype=np.float64, count=nparts)

    def write_config(self):
        """method to write configuration information for the ZOBOV run to file for later lookup"""

        info = 'handle = \'%s\'\nis_box = %s\nnum_tracers = %d\n' % (self.handle, self.is_box, self.num_tracers)
        info += 'num_mocks = %d\nnum_non_edge = %d\nbox_length = %f\n' % (self.num_mocks, self.num_non_edge,
                                                                          self.box_length)
        info += 'tracer_dens = %e' % self.tracer_dens
        info_file = self.output_folder + 'sample_info.txt'
        with open(info_file, 'w') as F:
            F.write(info)

    def read_config(self):
        """method to read configuration file for information about previous ZOBOV run"""

        info_file = self.output_folder + 'sample_info.txt'
        parms = imp.load_source('name', info_file)
        self.num_mocks = parms.num_mocks
        self.num_non_edge = parms.num_non_edge
        self.box_length = parms.box_length
        self.tracer_dens = parms.tracer_dens

    def zobov_wrapper(self, use_vozisol=False, zobov_box_div=2, zobov_buffer=0.1):
        """Wrapper function to call C-based ZOBOV codes

        Arguments:
            use_vozisol: flag to use vozisol.c to do tessellation (good for surveys)
            zobov_box_div: integer number of divisions of box, ignored if use_vozisol is True
            zobov_buffer: fraction of box length used as buffer region, ignored is use_vozisol is True

        """

        # ---run the tessellation--- #
        if use_vozisol:
            print("Calling vozisol to do the tessellation...")
            logfolder = self.output_folder + 'log/'
            if not os.access(logfolder, os.F_OK):
                os.makedirs(logfolder)
            logfile = logfolder + self.handle + '-zobov.out'
            log = open(logfile, "w")
            cmd = ["./bin/vozisol", self.posn_file, self.handle, str(self.box_length),
                   str(self.num_tracers), str(0.9e30)]
            subprocess.call(cmd, stdout=log, stderr=log)
            log.close()

            # check the tessellation was successful
            if not os.access("%s.vol" % self.handle, os.F_OK):
                sys.exit("Something went wrong with the tessellation. Aborting ...")
        else:
            print("Calling vozinit, voz1b1 and voztie to do the tessellation...")

            # ---Step 1: call vozinit to write the script used to call voz1b1 and voztie--- #
            logfolder = self.output_folder + 'log/'
            if not os.access(logfolder, os.F_OK):
                os.makedirs(logfolder)
            logfile = logfolder + self.handle + '.out'
            log = open(logfile, "w")
            cmd = ["./bin/vozinit", self.posn_file, str(zobov_buffer), str(self.box_length),
                   str(zobov_box_div), self.handle]
            subprocess.call(cmd, stdout=log, stderr=log)
            log.close()

            # ---Step 2: call this script to do the tessellation--- #
            voz_script = "scr" + self.handle
            cmd = ["./%s" % voz_script]
            log = open(logfile, 'a')
            subprocess.call(cmd, stdout=log, stderr=log)
            log.close()

            # ---Step 3: check the tessellation was successful--- #
            if not os.access("%s.vol" % self.handle, os.F_OK):
                sys.exit("Something went wrong with the tessellation. Aborting ...")

            # ---Step 4: remove the script file--- #
            if os.access(voz_script, os.F_OK):
                os.unlink(voz_script)

            # ---Step 5: copy the .vol files to .trvol--- #
            cmd = ["cp", "%s.vol" % self.handle, "%s.trvol" % self.handle]
            subprocess.call(cmd)

            # ---Step 6: if buffer mocks were used, remove them and flag edge galaxies--- #
            # (necessary because voz1b1 and voztie do not do this automatically)
            if self.num_mocks > 0:
                cmd = ["./bin/checkedges", self.handle, str(self.num_tracers), str(0.9e30)]
                log = open(logfile, 'a')
                subprocess.call(cmd, stdout=log, stderr=log)
                log.close()

        print("Tessellation done.\n")

        # ---prepare files for running jozov--- #
        if self.is_box:
            # no preparation is required for void-finding (no buffer mocks, no z-weights, no angular weights)
            if self.find_clusters:
                cmd = ["cp", "%s.vol" % self.handle, "%sc.vol" % self.handle]
                subprocess.call(cmd)
        else:
            # ---Step 1: read the edge-modified Voronoi volumes--- #
            with open('./%s.vol' % self.handle, 'r') as F:
                npreal = np.fromfile(F, dtype=np.int32, count=1)
                modvols = np.fromfile(F, dtype=np.float64, count=npreal)

            # ---Step 2: renormalize volumes in units of mean volume per galaxy--- #
            # (this step is necessary because otherwise the buffer mocks affect the calculation)
            edgemask = modvols == 1.0 / 0.9e30
            modvols[np.logical_not(edgemask)] *= (self.tracer_dens * self.box_length ** 3.) / self.num_part_total
            # check for failures!
            if np.any(modvols[np.logical_not(edgemask)] == 0):
                sys.exit('Tessellation gave some zero-volume Voronoi cells!!\nAborting...')

            # ---Step 3: scale volumes accounting for z-dependent selection--- #
            if self.use_z_wts:
                redshifts = self.tracers[:self.num_tracers, 5]
                selfnbins = np.loadtxt(self.selection_fn_file)
                selfn = InterpolatedUnivariateSpline(selfnbins[:, 0], selfnbins[:, 2], k=1)
                # smooth with a Savitzky-Golay filter to remove high-frequency noise
                x = np.linspace(redshifts.min(), redshifts.max(), 1000)
                y = savgol_filter(selfn(x), 101, 3)
                # then linearly interpolate the filtered interpolation
                selfn = InterpolatedUnivariateSpline(x, y, k=1)
                # scale the densities according to this
                modfactors = selfn(redshifts[np.logical_not(edgemask)])
                modvols[np.logical_not(edgemask)] *= modfactors
                # check for failures!
                if np.any(modvols[np.logical_not(edgemask)] == 0):
                    sys.exit('Use of z-weights caused some zero-volume Voronoi cells!!\nAborting...')

            # ---Step 4: scale volumes accounting for angular completeness--- #
            if self.use_ang_wts:
                ra = self.tracers[:self.num_tracers, 3]
                dec = self.tracers[:self.num_tracers, 4]
                # fetch the survey mask
                mask = hp.read_map(self.mask_file, verbose=False)
                nside = hp.get_nside(mask)
                # weight the densities by completeness
                pixels = hp.ang2pix(nside, np.deg2rad(90 - dec), np.deg2rad(ra))
                modfactors = mask[pixels]
                modvols[np.logical_not(edgemask)] *= modfactors[np.logical_not(edgemask)]
                if np.any(modvols[np.logical_not(edgemask)] == 0):
                    sys.exit('Use of angular weights caused some zero-volume Voronoi cells!!\nAborting...')

            # ---Step 5: write the scaled volumes to file--- #
            with open("./%s.vol" % self.handle, 'w') as F:
                npreal.tofile(F, format="%d")
                modvols.tofile(F, format="%f")

            # ---Step 6: if finding clusters, create the files required--- #
            if self.find_clusters:
                modvols[edgemask] = 0.9e30
                # and write to c.vol file
                with open("./%sc.vol" % self.handle, 'w') as F:
                    npreal.tofile(F, format="%d")
                    modvols.tofile(F, format="%f")

            # ---Step 7: set the number of non-edge galaxies--- #
            self.num_non_edge = self.num_tracers - sum(edgemask)

        # ---run jozov to perform the void-finding--- #
        cmd = ["./bin/jozovtrvol", "v", self.handle, str(0), str(0)]
        log = open(logfile, 'a')
        subprocess.call(cmd)
        log.close()
        # this call to (modified version of) jozov sets NO density threshold, so
        # ALL voids are merged without limit and the FULL merged void heirarchy is
        # output to file; distinct voids are later obtained in post-processing

        # ---if finding clusters, run jozov again--- #
        if self.find_clusters:
            print("Additionally, running watershed cluster-finder")
            cmd = ["./bin/jozovtrvol", "c", self.handle, str(0), str(0)]
            log = open(logfile, 'a')
            subprocess.call(cmd)
            log.close()

        # ---clean up: remove unnecessary files--- #
        for fileName in glob.glob("./part." + self.handle + ".*"):
            os.unlink(fileName)

        # ---clean up: move all other files to appropriate directory--- #
        raw_dir = self.output_folder + "rawZOBOV/"
        if not os.access(raw_dir, os.F_OK):
            os.makedirs(raw_dir)
        for fileName in glob.glob("./" + self.handle + "*"):
            cmd = ["mv", fileName, "%s." % raw_dir]
            subprocess.call(cmd)

    def postprocess_voids(self):
        """Method to post-process raw ZOBOV output to obtain discrete set of non-overlapping voids. This method
        is hard-coded to NOT allow any void merging, since no objective (non-arbitrary) criteria can be defined
        to control merging, if allowed.

        """

        print('Post-processing voids ...\n')

        # ------------NOTE----------------- #
        # Actually, the current code is built from previous code that did have merging
        # functionality. This functionality is still technically present, but is controlled
        # by the following hard-coded parameters. If you know what you are doing, you can
        # change them.
        # --------------------------------- #
        dont_merge = True
        use_r_threshold = False
        r_threshold = 1.
        use_link_density_threshold = False
        link_density_threshold = 1.
        count_all_voids = True
        use_stripping = False
        strip_density_threshold = 1.
        if use_stripping:
            if (strip_density_threshold < self.min_dens_cut) or (strip_density_threshold < link_density_threshold):
                print('ERROR: incorrect use of strip_density_threshold\nProceeding with automatically corrected value')
                strip_density_threshold = max(self.min_dens_cut, link_density_threshold)
        # --------------------------------- #

        # the files with ZOBOV output
        zone_file = self.output_folder + 'rawZOBOV/' + self.handle + '.zone'
        void_file = self.output_folder + 'rawZOBOV/' + self.handle + '.void'
        list_file = self.output_folder + 'rawZOBOV/' + self.handle + '.txt'
        volumes_file = self.output_folder + 'rawZOBOV/' + self.handle + '.trvol'
        densities_file = self.output_folder + 'rawZOBOV/' + self.handle + '.vol'

        # new files after post-processing
        new_void_file = self.output_folder + self.void_prefix + ".void"
        new_list_file = self.output_folder + self.void_prefix + "_list.txt"

        # load the list of void candidates
        voidsread = np.loadtxt(list_file, skiprows=2)
        # sort in ascending order of minimum dens
        sorted_order = np.argsort(voidsread[:, 3])
        voidsread = voidsread[sorted_order]

        num_voids = len(voidsread[:, 0])
        vid = np.asarray(voidsread[:, 0], dtype=int)
        edgelist = np.asarray(voidsread[:, 1], dtype=int)
        vollist = voidsread[:, 4]
        numpartlist = np.asarray(voidsread[:, 5], dtype=int)
        rlist = voidsread[:, 9]

        # load the void hierarchy
        with open(void_file, 'r') as Fvoid:
            hierarchy = Fvoid.readlines()
        # sanity check
        nvoids = int(hierarchy[0])
        if nvoids != num_voids:
            sys.exit('Unequal void numbers in voidfile and listfile, %d and %d!' % (nvoids, num_voids))
        hierarchy = hierarchy[1:]

        # load the particle-zone info
        zonedata = np.loadtxt(zone_file, dtype='int', skiprows=1)

        # load the VTFE volume information
        with open(volumes_file, 'r') as File:
            npart = np.fromfile(File, dtype=np.int32, count=1)[0]
            if not npart == self.num_tracers:  # sanity check
                sys.exit('npart = %d in %s.trvol file does not match num_tracers = %d!'
                         % (npart, self.handle, self.num_tracers))
            vols = np.fromfile(File, dtype=np.float64, count=npart)

        # load the VTFE density information
        with open(densities_file, 'r') as File:
            npart = np.fromfile(File, dtype=np.int32, count=1)[0]
            if not npart == self.num_tracers:  # sanity check
                sys.exit("npart = %d in %s.vol file does not match num_tracers = %d!"
                         % (npart, self.handle, self.num_tracers))
            densities = np.fromfile(File, dtype=np.float64, count=npart)
            densities = 1. / densities

        # mean volume per particle in box (including all buffer mocks)
        meanvol_trc = (self.box_length ** 3.) / self.num_part_total

        # parse the list of structures, separating distinct voids and performing minimal pruning
        with open(new_void_file, 'w') as Fnewvoid:
            with open(new_list_file, 'w') as Fnewlist:

                # initialize variables
                counted_zones = np.empty(0, dtype=int)
                edge_flag = np.empty(0, dtype=int)
                wtd_avg_dens = np.empty(0, dtype=int)
                num__acc = 0

                for i in range(num_voids):
                    coredens = voidsread[i, 3]
                    voidline = hierarchy[sorted_order[i]].split()
                    pos = 1
                    num_zones_to_add = int(voidline[pos])
                    finalpos = pos + num_zones_to_add + 1
                    rval = float(voidline[pos + 1])
                    rstopadd = rlist[i]
                    num_adds = 0
                    if rval >= 1 and coredens < self.min_dens_cut and numpartlist[i] >= self.void_min_num \
                            and (count_all_voids or vid[i] not in counted_zones):
                        # this void passes basic pruning
                        add_more = True
                        num__acc += 1
                        zonelist = vid[i]
                        total_vol = vollist[i]
                        total_num_parts = numpartlist[i]
                        zonestoadd = []
                        while num_zones_to_add > 0 and add_more:  # more zones can potentially be added
                            zonestoadd = np.asarray(voidline[pos + 2:pos + num_zones_to_add + 2], dtype=int)
                            dens = rval * coredens
                            rsublist = rlist[np.in1d(vid, zonestoadd)]
                            volsublist = vollist[np.in1d(vid, zonestoadd)]
                            partsublist = numpartlist[np.in1d(vid, zonestoadd)]
                            if dont_merge or (use_link_density_threshold and dens > link_density_threshold) or \
                                    (use_r_threshold > 0 and max(rsublist) > r_threshold):
                                # cannot add these zones
                                rstopadd = rval
                                add_more = False
                                finalpos -= (num_zones_to_add + 1)
                            else:
                                # keep adding zones
                                zonelist = np.append(zonelist, zonestoadd)
                                num_adds += num_zones_to_add
                                total_vol += np.sum(volsublist)  #
                                total_num_parts += np.sum(partsublist)  #
                            pos += num_zones_to_add + 2
                            num_zones_to_add = int(voidline[pos])
                            rval = float(voidline[pos + 1])
                            if add_more:
                                finalpos = pos + num_zones_to_add + 1

                        counted_zones = np.append(counted_zones, zonelist)
                        if use_stripping:
                            member_ids = np.logical_and(densities[:] < strip_density_threshold,
                                                        np.in1d(zonedata, zonelist))
                        else:
                            member_ids = np.in1d(zonedata, zonelist)

                        # if using void "stripping" functionality, recalculate void volume and number of particles
                        if use_stripping:
                            total_vol = np.sum(vols[member_ids])
                            total_num_parts = len(vols[member_ids])

                        # check if the void is edge-contaminated (useful for observational surveys only)
                        if 1 in edgelist[np.in1d(vid, zonestoadd)]:
                            edge_flag = np.append(edge_flag, 1)
                        else:
                            edge_flag = np.append(edge_flag, 0)

                        # average density of member cells weighted by cell volumes
                        w_a_d = np.sum(vols[member_ids] * densities[member_ids]) / np.sum(vols[member_ids])
                        wtd_avg_dens = np.append(wtd_avg_dens, w_a_d)

                        # set the new line for the .void file
                        newvoidline = voidline[:finalpos]
                        if not add_more:
                            newvoidline.append(str(0))
                        newvoidline.append(str(rstopadd))
                        # write line to the output .void file
                        for j in range(len(newvoidline)):
                            Fnewvoid.write(newvoidline[j] + '\t')
                        Fnewvoid.write('\n')
                        if rstopadd > 10 ** 20:
                            rstopadd = -1  # only structures entirely surrounded by edge particles
                        # write line to the output _list.txt file
                        Fnewlist.write('%d\t%d\t%f\t%d\t%d\t%d\t%f\t%f\n' % (vid[i], int(voidsread[i, 2]), coredens,
                                                                             int(voidsread[i, 5]), num_adds + 1,
                                                                             total_num_parts, total_vol * meanvol_trc,
                                                                             rstopadd))

        # tidy up the files
        # insert first line with number of voids to the new .void file
        with open(new_void_file, 'r+') as Fnewvoid:
            old = Fnewvoid.read()
            Fnewvoid.seek(0)
            topline = "%d\n" % num__acc
            Fnewvoid.write(topline + old)

        # insert header to the _list.txt file
        listdata = np.loadtxt(new_list_file)
        header = '%d non-edge tracers in %s, %d voids\n' % (self.num_non_edge, self.handle, num__acc)
        header = header + 'VoidID CoreParticle CoreDens Zone#Parts Void#Zones Void#Parts VoidVol(Mpc/h^3) VoidDensRatio'
        np.savetxt(new_list_file, listdata, fmt='%d %d %0.6f %d %d %d %0.6f %0.6f', header=header)

        # now find void centres and create the void catalogue files
        edge_flag = self.find_void_circumcentres(num__acc, wtd_avg_dens, edge_flag)
        if self.use_barycentres:
            if not os.access(self.output_folder + "barycentres/", os.F_OK):
                os.makedirs(self.output_folder + "barycentres/")
            self.find_void_barycentres(num__acc, edge_flag, use_stripping, strip_density_threshold)

    def find_void_circumcentres(self, num_struct, wtd_avg_dens, edge_flag):
        """Method that checks a list of processed voids, finds the void minimum density centres and writes
        the void catalogue file.

        Arguments:
            num_struct: integer number of voids after pruning
            wtd_avg_dens: float array of shape (num_struct,), weighted average void densities from post-processing
            edge_flag: integer array of shape (num_struct,), edge contamination flags
        """

        print("Identified %d voids. Now extracting circumcentres ..." % num_struct)

        # set the filenames
        densities_file = self.output_folder + "rawZOBOV/" + self.handle + ".vol"
        adjacency_file = self.output_folder + "rawZOBOV/" + self.handle + ".adj"
        list_file = self.output_folder + self.void_prefix + "_list.txt"
        info_file = self.output_folder + self.void_prefix + "_cat.txt"

        # load the VTFE density information
        with open(densities_file, 'r') as File:
            npart = np.fromfile(File, dtype=np.int32, count=1)[0]
            if not npart == self.num_tracers:  # sanity check
                sys.exit("npart = %d in %s.vol file does not match num_tracers = %d!"
                         % (npart, self.handle, self.num_tracers))
            densities = np.fromfile(File, dtype=np.float64, count=npart)
            densities = 1. / densities

        # check whether tracer information is present, re-read in if required
        if not len(self.tracers) == self.num_part_total:
            self.reread_tracer_info()
        # extract the x,y,z positions of the galaxies only (no buffer mocks)
        positions = self.tracers[:self.num_tracers, :3]

        list_array = np.loadtxt(list_file)
        v_id = np.asarray(list_array[:, 0], dtype=int)
        corepart = np.asarray(list_array[:, 1], dtype=int)

        # read and assign adjacencies from ZOBOV output
        with open(adjacency_file, 'r') as AdjFile:
            npfromadj = np.fromfile(AdjFile, dtype=np.int32, count=1)
            if not npfromadj == self.num_tracers:
                sys.exit("npart = %d from adjacency file does not match num_tracers = %d!"
                         % (npfromadj, self.num_tracers))
            partadjs = [[] for i in range(npfromadj)]  # list of lists to record adjacencies - is there a better way?
            partadjcount = np.zeros(npfromadj, dtype=np.int32)  # counter to monitor adjacencies
            nadj = np.fromfile(AdjFile, dtype=np.int32, count=npfromadj)  # number of adjacencies for each particle
            # load up the adjacencies from ZOBOV output
            for i in range(npfromadj):
                numtomatch = np.fromfile(AdjFile, dtype=np.int32, count=1)
                if numtomatch > 0:
                    # particle numbers of adjacent particles
                    adjpartnumbers = np.fromfile(AdjFile, dtype=np.int32, count=numtomatch)
                    # keep track of how many adjacencies had already been assigned
                    oldcount = partadjcount[i]
                    newcount = oldcount + len(adjpartnumbers)
                    partadjcount[i] = newcount
                    # and now assign new adjacencies
                    partadjs[i][oldcount:newcount] = adjpartnumbers
                    # now also assign the reverse adjacencies
                    # (ZOBOV records only (i adj j) or (j adj i), not both)
                    for index in adjpartnumbers:
                        partadjs[index].append(i)
                    partadjcount[adjpartnumbers] += 1

        if self.is_box:
            info_output = np.zeros((num_struct, 9))
        else:
            info_output = np.zeros((num_struct, 11))
        circumcentre = np.empty(3)

        # loop over void cores, calculating circumcentres and writing to file
        for i in range(num_struct):
            # get adjacencies of the core particle
            coreadjs = partadjs[corepart[i]]
            adj_dens = densities[coreadjs]

            # get the 3 lowest density mutually adjacent neighbours of the core particle
            first_nbr = coreadjs[np.argmin(adj_dens)]
            mutualadjs = np.intersect1d(coreadjs, partadjs[first_nbr])
            if len(mutualadjs) == 0:
                circumcentre = np.asarray([0, 0, 0])
                edge_flag[i] = 2
            else:
                mutualadj_dens = densities[mutualadjs]
                second_nbr = mutualadjs[np.argmin(mutualadj_dens)]
                finaladjs = np.intersect1d(mutualadjs, partadjs[second_nbr])
                if len(finaladjs) == 0:  # something has gone wrong at tessellation stage!
                    circumcentre = np.asarray([0, 0, 0])
                    edge_flag[i] = 2
                else:  # can calculate circumcentre position
                    finaladj_dens = densities[finaladjs]
                    third_nbr = finaladjs[np.argmin(finaladj_dens)]

                    # collect positions of the vertices
                    vertex_pos = np.zeros((4, 3))
                    vertex_pos[0, :] = positions[corepart[i], :]
                    vertex_pos[1, :] = positions[first_nbr, :]
                    vertex_pos[2, :] = positions[second_nbr, :]
                    vertex_pos[3, :] = positions[third_nbr, :]
                    if self.is_box:  # need to adjust for periodic BC
                        shift_inds = abs(vertex_pos[0, 0] - vertex_pos[:, 0]) > self.box_length / 2.0
                        vertex_pos[shift_inds, 0] += self.box_length * np.sign(vertex_pos[0, 0] -
                                                                               vertex_pos[shift_inds, 0])
                        shift_inds = abs(vertex_pos[0, 1] - vertex_pos[:, 1]) > self.box_length / 2.0
                        vertex_pos[shift_inds, 1] += self.box_length * np.sign(vertex_pos[0, 1] -
                                                                               vertex_pos[shift_inds, 1])
                        shift_inds = abs(vertex_pos[0, 2] - vertex_pos[:, 2]) > self.box_length / 2.0
                        vertex_pos[shift_inds, 2] += self.box_length * np.sign(vertex_pos[0, 2] -
                                                                               vertex_pos[shift_inds, 2])

                    # solve for the circumcentre; for more details on this method and its stability,
                    # see http://www.ics.uci.edu/~eppstein/junkyard/circumcentre.html
                    a = np.bmat([[2 * np.dot(vertex_pos, vertex_pos.T), np.ones((4, 1))],
                                 [np.ones((1, 4)), np.zeros((1, 1))]])
                    b = np.hstack((np.sum(vertex_pos * vertex_pos, axis=1), np.ones((1))))
                    x = np.linalg.solve(a, b)
                    bary_coords = x[:-1]
                    circumcentre[:] = np.dot(bary_coords, vertex_pos)

            if self.is_box:
                # put centre coords back within the fiducial box if they have leaked out
                if circumcentre[0] < 0 or circumcentre[0] > self.box_length:
                    circumcentre[0] -= self.box_length * np.sign(circumcentre[0])
                if circumcentre[1] < 0 or circumcentre[1] > self.box_length:
                    circumcentre[1] -= self.box_length * np.sign(circumcentre[1])
                if circumcentre[2] < 0 or circumcentre[2] > self.box_length:
                    circumcentre[2] -= self.box_length * np.sign(circumcentre[2])

            # calculate void effective radius
            eff_rad = (3.0 * list_array[i, 6] / (4 * np.pi)) ** (1.0 / 3)

            # if required, write sky positions to file
            if self.is_box:
                info_output[i] = [v_id[i], circumcentre[0], circumcentre[1], circumcentre[2], eff_rad,
                                  (list_array[i, 2] - 1.), (wtd_avg_dens[i] - 1.),
                                  (wtd_avg_dens[i] - 1) * eff_rad ** 1.2,
                                  list_array[i, 7]]
            else:
                # convert void centre position to observer coordinates
                centre_obs = circumcentre - self.box_length / 2.0  # move back into observer coordinates
                rdist = np.linalg.norm(centre_obs)
                eff_angrad = np.degrees(eff_rad / rdist)
                # calculate the sky coordinates of the void centre
                # (this step also allows fallback check of undetected tessellation leakage)
                if (rdist >= self.cosmo.get_comoving_distance(self.z_min)) and (
                        rdist <= self.cosmo.get_comoving_distance(self.z_max)):
                    centre_redshift = self.cosmo.get_redshift(rdist)
                    centre_dec = 90 - np.degrees(np.arccos(centre_obs[2] / rdist))
                    centre_ra = np.degrees(np.arctan2(centre_obs[1], centre_obs[0]))
                    if centre_ra < 0:
                        centre_ra += 360  # to get RA in the range 0 to 360
                    mask = hp.read_map(self.mask_file, verbose=False)
                    nside = hp.get_nside(mask)
                    pixel = hp.ang2pix(nside, np.deg2rad(90 - centre_dec), np.deg2rad(centre_ra))
                    if mask[pixel] == 0:  # something has gone wrong at tessellation stage
                        centre_redshift = -1
                        centre_dec = -60
                        centre_ra = -60
                        eff_angrad = 0
                        edge_flag[i] = 2
                else:  # something has gone wrong at tessellation stage
                    centre_redshift = -1
                    centre_dec = -60
                    centre_ra = -60
                    eff_angrad = 0
                    edge_flag[i] = 2
                info_output[i] = [v_id[i], centre_ra, centre_dec, centre_redshift, eff_rad, (list_array[i, 2] - 1.),
                                  (wtd_avg_dens[i] - 1.), (wtd_avg_dens[i] - 1) * eff_rad ** 1.2, list_array[i, 7],
                                  eff_angrad, edge_flag[i]]

        # save output data to file
        header = "%d voids from %s\n" % (num_struct, self.handle)
        if self.is_box:
            header = header + 'VoidID XYZ[3](Mpc/h) R_eff(Mpc/h) delta_min delta_avg lambda_v DensRatio'
            np.savetxt(info_file, info_output, fmt='%d %0.6f %0.6f %0.6f %0.3f %0.6f %0.6f %0.6f %0.6f', header=header)
        else:
            header = header + 'VoidID RA(deg) Dec(deg) redshift R_eff(Mpc/h) delta_min delta_avg lambda_v ' + \
                     'DensRatio Theta_eff(deg) EdgeFlag'
            np.savetxt(info_file, info_output, fmt='%d %0.6f %0.3f %0.3f %0.4f %0.3f %0.6f %0.6f %0.6f %0.6f %d',
                       header=header)

        return edge_flag

    def find_void_barycentres(self, num_struct, edge_flag, use_stripping=False, strip_density_threshold=1.):
        """Method that checks a list of processed voids, finds the void barycentres and writes
        the void catalogue file.

        Arguments:
            num_struct: integer number of voids after pruning
            edge_flag: integer array of shape (num_struct,), edge contamination flags
            use_stripping: bool,optional (default is False, don't change unless you know what you're doing!)
            strip_density_threshold: float, optional (default 1.0, not required unless use_stripping is True)
        """

        print('Now extracting void barycentres ...\n')

        # set the filenames
        vol_file = self.output_folder + 'rawZOBOV/' + self.handle + '.trvol'
        dens_file = self.output_folder + 'rawZOBOV/' + self.handle + '.vol'
        zone_file = self.output_folder + 'rawZOBOV/' + self.handle + '.zone'
        hierarchy_file = self.output_folder + self.void_prefix + '.void'
        list_file = self.output_folder + self.void_prefix + '_list.txt'
        info_file = self.output_folder + 'barycentres/' + self.void_prefix + '_baryC_cat.txt'

        # load up the particle-zone info
        zonedata = np.loadtxt(zone_file, dtype='int', skiprows=1)

        # load the VTFE volume information
        with open(vol_file, 'r') as File:
            npart = np.fromfile(File, dtype=np.int32, count=1)[0]
            if not npart == self.num_tracers:  # sanity check
                sys.exit('npart = %d in %s.trvol file does not match num_tracers = %d!'
                         % (npart, self.handle, self.num_tracers))
            vols = np.fromfile(File, dtype=np.float64, count=npart)

        # load the VTFE density information
        with open(dens_file, 'r') as File:
            npart = np.fromfile(File, dtype=np.int32, count=1)[0]
            if not npart == self.num_tracers:  # sanity check
                sys.exit("npart = %d in %s.vol file does not match num_tracers = %d!"
                         % (npart, self.handle, self.num_tracers))
            densities = np.fromfile(File, dtype=np.float64, count=npart)
            densities = 1. / densities

        # mean volume per particle in box (including all buffer mocks)
        meanvol_trc = (self.box_length ** 3.) / self.num_part_total

        # check whether tracer information is present, re-read in if required
        if not len(self.tracers) == self.num_part_total:
            self.reread_tracer_info()
        # extract the x,y,z positions of the galaxies only (no buffer mocks)
        positions = self.tracers[:self.num_tracers, :3]

        list_array = np.loadtxt(list_file, skiprows=2)
        if self.is_box:
            info_output = np.zeros((num_struct, 9))
        else:
            info_output = np.zeros((num_struct, 11))
        with open(hierarchy_file, 'r') as FHierarchy:
            FHierarchy.readline()  # skip the first line, contains total number of structures
            for i in range(num_struct):
                # get the member zones of the structure
                structline = (FHierarchy.readline()).split()
                pos = 1
                add_zones = int(structline[pos]) > 0
                member_zones = np.asarray(structline[0], dtype=int)
                while add_zones:
                    num_zones_to_add = int(structline[pos])
                    zonestoadd = np.asarray(structline[pos + 2:pos + num_zones_to_add + 2], dtype=int)
                    member_zones = np.append(member_zones, zonestoadd)
                    pos += num_zones_to_add + 2
                    add_zones = int(structline[pos]) > 0

                # get the member particles for these zones
                if use_stripping:
                    member_ids = np.logical_and(densities[:] < strip_density_threshold, np.in1d(zonedata, member_zones))
                else:  # stripDens functionality disabled
                    member_ids = np.in1d(zonedata, member_zones)
                member_x = positions[member_ids, 0] - positions[int(list_array[i, 1]), 0]
                member_y = positions[member_ids, 1] - positions[int(list_array[i, 1]), 1]
                member_z = positions[member_ids, 2] - positions[int(list_array[i, 1]), 2]
                member_vols = vols[member_ids]
                member_dens = densities[member_ids]

                if self.is_box:
                    # must account for periodic boundary conditions, assume box coordinates in range [0,box_length]!
                    shift_vec = np.zeros((len(member_x), 3))
                    shift_x_ids = abs(member_x) > self.box_length / 2.0
                    shift_y_ids = abs(member_y) > self.box_length / 2.0
                    shift_z_ids = abs(member_z) > self.box_length / 2.0
                    shift_vec[shift_x_ids, 0] = -np.copysign(self.box_length, member_x[shift_x_ids])
                    shift_vec[shift_y_ids, 1] = -np.copysign(self.box_length, member_y[shift_y_ids])
                    shift_vec[shift_z_ids, 2] = -np.copysign(self.box_length, member_z[shift_z_ids])
                    member_x += shift_vec[:, 0]
                    member_y += shift_vec[:, 1]
                    member_z += shift_vec[:, 2]

                # volume-weighted barycentre of the structure
                centre = np.empty(3)
                centre[0] = np.sum(member_x * member_vols / np.sum(member_vols)) + positions[int(list_array[i, 1]), 0]
                centre[1] = np.sum(member_y * member_vols / np.sum(member_vols)) + positions[int(list_array[i, 1]), 1]
                centre[2] = np.sum(member_z * member_vols / np.sum(member_vols)) + positions[int(list_array[i, 1]), 2]

                # put centre coords back within the fiducial box if they have leaked out
                if self.is_box:
                    if centre[0] < 0 or centre[0] > self.box_length:
                        centre[0] -= self.box_length * np.sign(centre[0])
                    if centre[1] < 0 or centre[1] > self.box_length:
                        centre[1] -= self.box_length * np.sign(centre[1])
                    if centre[2] < 0 or centre[2] > self.box_length:
                        centre[2] -= self.box_length * np.sign(centre[2])

                # total volume of structure in Mpc/h, and effective radius
                void_vol = np.sum(member_vols) * meanvol_trc
                eff_rad = (3.0 * void_vol / (4 * np.pi)) ** (1.0 / 3)

                # average density of member cells weighted by cell volumes
                wtd_avg_dens = np.sum(member_dens * member_vols) / np.sum(member_vols)

                lambda_v = (wtd_avg_dens - 1) * eff_rad ** 1.2

                # if required, write sky positions to file
                if self.is_box:
                    info_output[i] = [list_array[i, 0], centre[0], centre[1], centre[2], eff_rad,
                                      (list_array[i, 2] - 1.),
                                      (wtd_avg_dens - 1.), lambda_v, list_array[i, 7]]
                else:
                    centre_obs = centre - self.box_length / 2.0  # move back into observer coordinates
                    rdist = np.linalg.norm(centre_obs)
                    eff_angrad = np.degrees(eff_rad / rdist)
                    if (rdist >= self.cosmo.get_comoving_distance(self.z_min)) and (
                            rdist <= self.cosmo.get_comoving_distance(self.z_max)):
                        centre_redshift = self.cosmo.get_redshift(rdist)
                        centre_dec = 90 - np.degrees(np.arccos(centre_obs[2] / rdist))
                        centre_ra = np.degrees(np.arctan2(centre_obs[1], centre_obs[0]))
                        if centre_ra < 0:
                            centre_ra += 360  # to get RA in the range 0 to 360
                        mask = hp.read_map(self.mask_file, verbose=False)
                        nside = hp.get_nside(mask)
                        pixel = hp.ang2pix(nside, np.deg2rad(90 - centre_dec), np.deg2rad(centre_ra))
                        if mask[pixel] == 0:  # something has gone wrong at tessellation stage
                            centre_redshift = -1
                            centre_dec = -60
                            centre_ra = -60
                            eff_angrad = 0
                            edge_flag[i] = 2
                    else:  # something has gone wrong at tessellation stage
                        centre_redshift = -1
                        centre_dec = -60
                        centre_ra = -60
                        eff_angrad = 0
                        edge_flag[i] = 2
                    info_output[i] = [list_array[i, 0], centre_ra, centre_dec, centre_redshift, eff_rad,
                                      (list_array[i, 2] - 1.), (wtd_avg_dens - 1.), lambda_v, list_array[i, 7],
                                      eff_angrad, edge_flag[i]]

        # save output data to file
        header = "%d voids from %s\n" % (num_struct, self.handle)
        if self.is_box:
            header = header + 'VoidID XYZ[3](Mpc/h) R_eff(Mpc/h) delta_min delta_avg lambda_v DensRatio'
            np.savetxt(info_file, info_output, fmt='%d %0.6f %0.6f %0.6f %0.3f %0.6f %0.6f %0.6f %0.6f', header=header)
        else:
            header = header + 'VoidID RA(deg) Dec(deg) redshift R_eff(Mpc/h) delta_min delta_avg lambda_v' + \
                     'DensRatio Theta_eff(deg) EdgeFlag'
            np.savetxt(info_file, info_output, fmt='%d %0.6f %0.3f %0.3f %0.4f %0.3f %0.6f %0.6f %0.6f %0.6f %d',
                       header=header)

    def postprocess_clusters(self):
        """
        Method to post-process raw ZOBOV output to obtain discrete set of non-overlapping 'superclusters'. This method
        is hard-coded to NOT allow any supercluster merging, since no objective (non-arbitrary) criteria can be defined
        to control merging anyway.
        """

        print('Post-processing superclusters ...\n')

        # ------------NOTE----------------- #
        # Actually, the current code is built from previous code that did have merging
        # functionality. This functionality is still technically present, but is controlled
        # by the following hard-coded parameters. If you know what you are doing, you can
        # change them.
        # --------------------------------- #
        dont_merge = True
        use_r_threshold = False
        r_threshold = 2.
        use_link_density_threshold = False
        link_density_threshold = 1.
        count_all_clusters = True
        use_stripping = False
        strip_density_threshold = 1.
        if use_stripping:
            if (strip_density_threshold > self.max_dens_cut) or (strip_density_threshold > link_density_threshold):
                print('ERROR: incorrect use of strip_density_threshold\nProceeding with automatically corrected value')
                strip_density_threshold = max(self.max_dens_cut, link_density_threshold)
        # --------------------------------- #

        # the files with ZOBOV output
        zone_file = self.output_folder + "rawZOBOV/" + self.handle + "c.zone"
        clust_file = self.output_folder + "rawZOBOV/" + self.handle + "c.void"
        list_file = self.output_folder + "rawZOBOV/" + self.handle + "c.txt"
        vol_file = self.output_folder + "rawZOBOV/" + self.handle + ".trvol"
        dens_file = self.output_folder + "rawZOBOV/" + self.handle + ".vol"
        info_file = self.output_folder + self.cluster_prefix + "_cat.txt"

        # new files after post-processing
        new_clust_file = self.output_folder + self.cluster_prefix + ".void"
        new_list_file = self.output_folder + self.cluster_prefix + "_list.txt"

        # load the list of supercluster candidates
        clustersread = np.loadtxt(list_file, skiprows=2)
        # sort in desc order of max dens
        sorted_order = np.argsort(1. / clustersread[:, 3])
        clustersread = clustersread[sorted_order]

        num_clusters = len(clustersread[:, 0])
        vid = np.asarray(clustersread[:, 0], dtype=int)
        edgelist = np.asarray(clustersread[:, 1], dtype=int)
        vollist = clustersread[:, 4]
        numpartlist = np.asarray(clustersread[:, 5], dtype=int)
        rlist = clustersread[:, 9]

        # load up the cluster hierarchy
        with open(clust_file, 'r') as Fclust:
            hierarchy = Fclust.readlines()
        nclusters = int(hierarchy[0])
        if nclusters != num_clusters:
            sys.exit('Unequal void numbers in clustfile and listfile, %d and %d!' % (nclusters, num_clusters))
        hierarchy = hierarchy[1:]

        # load up the particle-zone info
        zonedata = np.loadtxt(zone_file, dtype='int', skiprows=1)

        # load the VTFE volume information
        with open(vol_file, 'r') as File:
            npart = np.fromfile(File, dtype=np.int32, count=1)[0]
            if not npart == self.num_tracers:  # sanity check
                sys.exit('npart = %d in %s.trvol file does not match num_tracers = %d!'
                         % (npart, self.handle, self.num_tracers))
            vols = np.fromfile(File, dtype=np.float64, count=npart)

        # load the VTFE density information
        with open(dens_file, 'r') as File:
            npart = np.fromfile(File, dtype=np.int32, count=1)[0]
            if not npart == self.num_tracers:  # sanity check
                sys.exit("npart = %d in %s.cvol file does not match num_tracers = %d!"
                         % (npart, self.handle, self.num_tracers))
            densities = np.fromfile(File, dtype=np.float64, count=npart)
            densities = 1. / densities

        # check whether tracer information is present, re-read in if required
        if not len(self.tracers) == self.num_part_total:
            self.reread_tracer_info()
        # extract the x,y,z positions of the galaxies only (no buffer mocks)
        positions = self.tracers[:self.num_tracers, :3]

        # mean volume per tracer particle
        meanvol_trc = (self.box_length ** 3.) / self.num_part_total

        with open(new_clust_file, 'w') as Fnewclust:
            with open(new_list_file, 'w') as Fnewlist:

                # initialize variables
                counted_zones = np.empty(0, dtype=int)
                edge_flag = np.empty(0, dtype=int)
                wtd_avg_dens = np.empty(0, dtype=int)
                num__acc = 0

                for i in range(num_clusters):
                    coredens = clustersread[i, 3]
                    clustline = hierarchy[sorted_order[i]].split()
                    pos = 1
                    num_zones_to_add = int(clustline[pos])
                    finalpos = pos + num_zones_to_add + 1
                    rval = float(clustline[pos + 1])
                    rstopadd = rlist[i]
                    num_adds = 0
                    if rval >= 1 and coredens > self.max_dens_cut and numpartlist[i] >= self.cluster_min_num \
                            and (count_all_clusters or vid[i] not in counted_zones):
                        # this zone qualifies as a seed zone
                        add_more = True
                        num__acc += 1
                        zonelist = [vid[i]]
                        total_vol = vollist[i]
                        total_num_parts = numpartlist[i]
                        zonestoadd = []
                        while num_zones_to_add > 0 and add_more:
                            zonestoadd = np.asarray(clustline[pos + 2:pos + num_zones_to_add + 2], dtype=int)
                            dens = coredens / rval
                            rsublist = rlist[np.in1d(vid, zonestoadd)]
                            volsublist = vollist[np.in1d(vid, zonestoadd)]
                            partsublist = numpartlist[np.in1d(vid, zonestoadd)]
                            if dont_merge or (use_link_density_threshold and dens < link_density_threshold) or \
                                    (use_r_threshold and max(rsublist) > r_threshold):
                                # cannot add these zones
                                rstopadd = rval
                                add_more = False
                                finalpos -= (num_zones_to_add + 1)
                            else:
                                # keep adding zones
                                zonelist = np.append(zonelist, zonestoadd)
                                num_adds += num_zones_to_add
                                total_vol += np.sum(volsublist)
                                total_num_parts += np.sum(partsublist)
                            pos += num_zones_to_add + 2
                            num_zones_to_add = int(clustline[pos])
                            rval = float(clustline[pos + 1])
                            if add_more:
                                finalpos = pos + num_zones_to_add + 1

                        counted_zones = np.append(counted_zones, zonelist)
                        member_ids = np.logical_and(
                            np.logical_or(use_stripping, densities[:] > strip_density_threshold),
                            np.in1d(zonedata, zonelist))
                        if use_stripping:  # need to recalculate total_vol and total_num_parts after stripping
                            total_vol = np.sum(vols[member_ids])
                            total_num_parts = len(vols[member_ids])

                        if 1 in edgelist[np.in1d(vid, zonestoadd)]:
                            edge_flag = np.append(edge_flag, 1)
                        else:
                            edge_flag = np.append(edge_flag, 0)

                        # average density of member cells weighted by cell volumes
                        w_a_d = np.sum(vols[member_ids] * densities[member_ids]) / np.sum(vols[member_ids])
                        wtd_avg_dens = np.append(wtd_avg_dens, w_a_d)

                        newclustline = clustline[:finalpos]
                        if not add_more:
                            newclustline.append(str(0))
                        newclustline.append(str(rstopadd))

                        # write line to the output .void file
                        for j in range(len(newclustline)):
                            Fnewclust.write(newclustline[j] + '\t')
                        Fnewclust.write('\n')

                        if rstopadd > 10 ** 20:
                            rstopadd = -1  # will be true for structures entirely surrounded by edge particles
                        # write line to the output _list.txt file
                        Fnewlist.write('%d\t%d\t%f\t%d\t%d\t%d\t%f\t%f\n' % (vid[i], int(clustersread[i, 2]), coredens,
                                                                             int(clustersread[i, 5]), num_adds + 1,
                                                                             total_num_parts, total_vol * meanvol_trc,
                                                                             rstopadd))

        # tidy up the files
        # insert first line with number of clusters to the new .void file
        with open(new_clust_file, 'r+') as Fnewclust:
            old = Fnewclust.read()
            Fnewclust.seek(0)
            topline = "%d\n" % num__acc
            Fnewclust.write(topline + old)

        # insert header to the output _list.txt file
        listdata = np.loadtxt(new_list_file)
        header = "%d non-edge tracers in %s, %d clusters\n" % (self.num_non_edge, self.handle, num__acc)
        header = header + "ClusterID CoreParticle CoreDens Zone#Parts Cluster#Zones Cluster#Parts" + \
                 "ClusterVol(Mpc/h^3) ClusterDensRatio"
        np.savetxt(new_list_file, listdata, fmt='%d %d %0.6f %d %d %d %0.6f %0.6f', header=header)

        # now find the maximum density centre locations of the superclusters
        list_array = np.loadtxt(new_list_file)
        if self.is_box:
            info_output = np.zeros((num__acc, 9))
        else:
            info_output = np.zeros((num__acc, 11))
        with open(new_clust_file, 'r') as FHierarchy:
            FHierarchy.readline()  # skip the first line, contains total number of structures
            for i in range(num__acc):
                # get the member zones of the structure
                structline = (FHierarchy.readline()).split()
                pos = 1
                add_zones = int(structline[pos]) > 0
                member_zones = np.asarray(structline[0], dtype=int)
                while add_zones:
                    num_zones_to_add = int(structline[pos])
                    zonestoadd = np.asarray(structline[pos + 2:pos + num_zones_to_add + 2], dtype=int)
                    member_zones = np.append(member_zones, zonestoadd)
                    pos += num_zones_to_add + 2
                    add_zones = int(structline[pos]) > 0

                # get the member particles for these zones
                if use_stripping:
                    member_ids = np.logical_and(densities[:] > strip_density_threshold, np.in1d(zonedata, member_zones))
                else:  # stripDens functionality disabled
                    member_ids = np.in1d(zonedata, member_zones)
                member_vol = vols[member_ids]
                member_dens = densities[member_ids]

                # centre location is position of max. density member particle
                core_part_id = int(list_array[i, 1])
                centre = positions[core_part_id]

                # total volume of structure in Mpc/h, and effective radius
                cluster_vol = np.sum(member_vol) * meanvol_trc
                eff_rad = (3.0 * cluster_vol / (4 * np.pi)) ** (1.0 / 3)

                # average density of member cells weighted by cell volumes
                wtd_avg_dens = np.sum(member_dens * member_vol) / np.sum(member_vol)

                if self.is_box:
                    info_output[i] = [list_array[i, 0], centre[0], centre[1], centre[2], eff_rad, list_array[i, 2],
                                      wtd_avg_dens, (wtd_avg_dens - 1) * eff_rad ** 1.6, list_array[i, 7]]
                else:
                    centre_obs = centre - self.box_length / 2.0  # move back into observer coordinates
                    rdist = np.linalg.norm(centre_obs)
                    centre_redshift = self.cosmo.get_redshift(rdist)
                    centre_dec = 90 - np.degrees(np.arccos(centre_obs[2] / rdist))
                    centre_ra = np.degrees(np.arctan2(centre_obs[1], centre_obs[0]))
                    if centre_ra < 0:
                        centre_ra += 360  # to get RA in the range 0 to 360
                    eff_ang_rad = np.degrees(eff_rad / rdist)
                    info_output[i] = [list_array[i, 0], centre_ra, centre_dec, centre_redshift, eff_rad,
                                      list_array[i, 2],
                                      wtd_avg_dens, (wtd_avg_dens - 1) * eff_rad ** 1.6, list_array[i, 7],
                                      eff_ang_rad, edge_flag[i]]

        # save output data to file
        header = "%d superclusters from %s\n" % (num__acc, self.handle)
        if self.is_box:
            header = header + 'ClusterID XYZ[3](Mpc/h) R_eff(Mpc/h) delta_max delta_avg lambda_c DensRatio'
            np.savetxt(info_file, info_output, fmt='%d %0.6f %0.6f %0.6f %0.6f %0.6f %0.6f %0.6f %0.6f %d %d',
                       header=header)
        else:
            header = header + 'ClusterID RA(deg) Dec(deg) redshift R_eff(Mpc/h) delta_max delta_avg lambda_c ' + \
                     'DensRatio Theta_eff(deg) EdgeFlag'
            np.savetxt(info_file, info_output, fmt='%d %0.6f %0.6f %0.6f %0.6f %0.6f %0.6f %0.6f %0.6f %0.6f %d',
                       header=header)