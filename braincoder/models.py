import tensorflow as tf
import tensorflow_probability as tfp
import logging
import pandas as pd
import numpy as np
from tqdm import tqdm
from .utils import norm, format_data, format_paradigm, format_parameters, format_weights, logit, restrict_radians
from tensorflow_probability import distributions as tfd
from tensorflow.math import softplus, sigmoid
import pandas as pd


class EncodingModel(object):

    parameter_labels = None

    def __init__(self, paradigm=None, data=None, parameters=None,
                 weights=None, omega=None, verbosity=logging.INFO):

        if (self.parameter_labels is not None) & (type(parameters) is pd.DataFrame):
            parameters = parameters[self.parameter_labels]

        self.paradigm = paradigm
        self.data = data
        self.parameters = parameters
        self.weights = weights
        self.omega = omega

        if omega is not None:
            self.omega_chol = np.linalg.cholesky(omega)

    @tf.function
    def _predict(self, paradigm, parameters, weights=None):

        # paradigm: n_batch x n_timepoints x n_stimulus_features
        # parameters: n_batch x n_units x n_parameters
        # weights: n_batch x n_basis_functions x n_units

        # returns: n_batch x n_timepoints x n_units
        if weights is None:
            return self._basis_predictions(paradigm, parameters)
        else:
            return tf.tensordot(self._basis_predictions(paradigm, parameters), weights, (2, 1))[:, :, 0, :]

    def predict(self, paradigm=None, parameters=None, weights=None):

        weights, weights_ = self._get_weights(weights)

        if paradigm is None:
            if self.paradigm is None:
                raise Exception('Need to set paradigm')
            else:
                paradigm = self.paradigm
        else:
            paradigm = format_paradigm(paradigm)

        if parameters is None:
            if self.parameters is None:
                raise Exception('Need to set parameters')
            else:
                parameters = self.parameters

        predictions = self._predict(
            paradigm.values[np.newaxis, ...], parameters.values[np.newaxis, ...], weights_)[0]

        print(predictions.shape)

        if weights is None:
            return pd.DataFrame(predictions.numpy(), index=paradigm.index, columns=parameters.index)
        else:
            return pd.DataFrame(predictions.numpy(), index=paradigm.index, columns=weights.columns)

    def simulate(self, paradigm=None, parameters=None, weights=None, noise=1.):

        weights, weights_ = self._get_weights(weights)

        if paradigm is None:
            paradigm = self.paradigm
        else:
            paradigm = format_paradigm(paradigm)

        if parameters is None:
            parameters = self.parameters
        else:
            parameters = format_parameters(parameters)

        simulated_data = self._simulate(
            paradigm.values[np.newaxis, ...],
            parameters.values[np.newaxis, ...],
            weights_, noise).numpy()

        print(simulated_data.shape)

        if weights is None:
            return pd.DataFrame(simulated_data[0], index=paradigm.index, columns=parameters.index)
        else:
            return pd.DataFrame(simulated_data[0], index=paradigm.index, columns=weights.columns)

    def _simulate(self, paradigm, parameters, weights, noise=1.):

        n_batches = paradigm.shape[0]
        n_timepoints = paradigm.shape[1]

        if weights is None:
            n_voxels = parameters.shape[1]
        else:
            n_voxels = weights.shape[2]

        noise = tf.random.normal(shape=(n_batches, n_timepoints, n_voxels),
                                 mean=0.0,
                                 stddev=noise,
                                 dtype=tf.float32)

        return self._predict(paradigm, parameters, weights) + noise

    @property
    def data(self):
        return self._data

    @data.setter
    def data(self, data):
        if data is None:
            self._data = None
        else:
            self._data = format_data(data)

    @property
    def paradigm(self):
        return self._paradigm

    @paradigm.setter
    def paradigm(self, paradigm):
        self._paradigm = format_paradigm(paradigm)

    @property
    def parameters(self):
        return self._parameters

    @parameters.setter
    def parameters(self, parameters):
        self._parameters = format_parameters(parameters, self.parameter_labels)

    @property
    def weights(self):
        return self._weights

    @weights.setter
    def weights(self, weights):
        self._weights = format_weights(weights)

    def to_discrete_model(self, grid, parameters=None, weights=None):

        grid = np.array(grid, dtype=np.float32)[:, np.newaxis]
        parameters = format_parameters(parameters)
        weights = format_weights(weights)

        if parameters is None:
            parameters = self.parameters

        if weights is None:
            weights = self.weights

        if weights is not None:
            weights = weights.value

        discrete_weights = self._predict(grid, parameters.values, weights)

        return DiscreteModel(paradigm=self.paradigm,
                             parameters=np.diag(grid[:, 0]),
                             weights=discrete_weights,
                             data=self.data)

    def likelihood(self, stimuli, data=None, parameters=None, weights=None, omega=None, dof=None, logp=False, normalize=True):

        if data is None:
            data = self.data
        else:
            data = format_data(data)

        if parameters is None:
            parameters = self.parameters
        else:
            parameters = format_parameters(parameters)

        if not isinstance(stimuli, pd.DataFrame):
            stimuli = pd.DataFrame(stimuli)
            stimuli.index.name = 'stimulus'
            stimuli.columns.name = 'stimulus dimension'

        for name, value in zip(['data', 'parameters'], [data, parameters]):
            if value is None:
                raise Exception('Please set {}'.format(name))

        omega_chol = tf.linalg.cholesky(omega).numpy()

        likelihood = self._likelihood(stimuli.values, data.values, parameters.values,
                                      weights if not hasattr(
                                          weights, 'values') else weights.values,
                                      omega_chol,
                                      dof,
                                      logp,
                                      normalize).numpy()

        likelihood = pd.DataFrame(
            likelihood, index=data.index, columns=stimuli.index)

        return likelihood

    def get_stimulus_pdf(self, data, stimulus_range, parameters=None, weights=None, omega=None, dof=None):

        if hasattr(data, 'values'):
            time_index = data.index
            data = data.values
        else:
            time_index = pd.Index(np.arange(len(data)), name='frame')

        if parameters is None:
            parameters = self.parameters

        if hasattr(parameters, 'values'):
            paramaters = parameters.values

        if omega is None:
            omega = self.omega

        weights, weights_ = self._get_weights(weights)

        ll = self._likelihood(stimulus_range[:, np.newaxis, np.newaxis],
                              data[np.newaxis, :, :],
                              parameters.values[np.newaxis, :, :],
                              weights_,
                              omega,
                              dof,
                              logp=True,
                              normalize=False).numpy()

        ll = pd.DataFrame(ll.T, index=time_index, columns=pd.Index(
            stimulus_range, name='stimulus'))

        # Normalize, working from log likelihoods (otherwise we get numerical issues)
        ll = np.exp(ll.apply(lambda d: d-d.max(), 1))
        ll = ll.apply(lambda d: d/d.sum(), axis=1)

        return ll

    def apply_mask(self, mask):

        if self.data is not None:
            self.data = self.data.loc[:, mask]

        if self.weights is None:
            if self.parameters is not None:
                self.parameters = self.parameters.loc[mask]
        else:
            self.weights = self.weights.loc[:, mask]

    def get_WWT(self):
        return self.weights.T.dot(self.weights)

    def get_residual_dist(self, n_voxels, omega_chol, dof):

        if dof is None:
            residual_dist = tfd.MultivariateNormalTriL(
                tf.zeros(n_voxels),
                scale_tril=omega_chol, allow_nan_stats=False)
        else:
            residual_dist = tfd.MultivariateStudentTLinearOperator(
                dof,
                tf.zeros(n_voxels),
                tf.linalg.LinearOperatorLowerTriangular(omega_chol), allow_nan_stats=False)

        return residual_dist

    @tf.function
    def _likelihood(self, stimuli, data, parameters, weights, omega_chol, dof, logp=False, normalize=False):

        # stimuli: n_batches x n_timepoints x n_stimulus_features
        # data: n_batches x n_timepoints x n_units
        # parameters: n_batches x n_subpops x n_parmeters
        # weights: n_batches x n_subpops x n_units
        # omega: n_units x n_units

        # n_batches * n_timepoints x n_stimulus_features
        prediction = self._predict(stimuli, parameters, weights)

        return self._likelihood_timeseries(data, prediction, omega_chol, dof, logp, normalize)

    @tf.function
    def _likelihood_timeseries(self, data, prediction, omega_chol, dof, logp=False, normalize=False):
        # n_timepoints x n_stimuli x n_units
        n_units = data.shape[2]

        residuals = data - prediction
        residual_dist = self.get_residual_dist(n_units, omega_chol, dof)

        # we use log likelihood to correct for very small numbers
        p = residual_dist.log_prob(residuals)

        if logp:
            return p

        if normalize:
            p = p - tf.reduce_max(p, 1)[:, tf.newaxis]
            p = tf.exp(p)
            p = p / tf.reduce_sum(p, 1)[:, tf.newaxis]
        else:
            p = tf.exp(p)

        return p

    def _get_weights(self, weights=None):

        if (weights is None) and (self.weights is not None):
            weights = self.weights

        weights = format_weights(weights)

        if weights is None:
            weights_ = weights
        else:
            weights_ = weights.values[np.newaxis, ...]

        return weights, weights_


class HRFEncodingModel(EncodingModel):

    def __init__(self, paradigm=None, data=None, parameters=None,
                 weights=None, omega=None, hrf_model=None, verbosity=logging.INFO, **kwargs):

        if hrf_model is None:
            raise ValueError('Please provide HRFModel!')

        self.hrf_model = hrf_model

        super().__init__(paradigm, data, parameters, weights, omega, verbosity, **kwargs)

    @tf.function
    def _predict(self, paradigm, parameters, weights):
        pre_convolve = EncodingModel._predict(
            self, paradigm, parameters, weights)

        return self.hrf_model.convolve(pre_convolve)

    @tf.function
    def _predict_no_hrf(self, paradigm, parameters, weights):
        return EncodingModel._predict(self, paradigm, parameters, weights)

    def get_init_pars(self, data, paradigm, confounds=None):

        paradigm_shift = tf.cast(tf.math.round(
            self.hrf_model.delay / self.hrf_model.tr), tf.int32)

        padding = [[paradigm_shift, 0], [0, 0]]

        paradigm = tf.pad(paradigm, padding)[:-paradigm_shift]

        return super().get_init_pars(data, paradigm, confounds)


class GaussianPRF(EncodingModel):

    parameter_labels = ['mu', 'sd', 'amplitude', 'baseline']

    def __init__(self, paradigm=None, data=None, parameters=None,
                 weights=None, omega=None, allow_neg_amplitudes=False, verbosity=logging.INFO,
                 **kwargs):
    
        super().__init__(paradigm=paradigm, data=data, parameters=parameters,
                         weights=weights, omega=omega, verbosity=logging.INFO, **kwargs)

        if allow_neg_amplitudes:
            self._transform_parameters_forward = self._transform_parameters_forward1
            self._transform_parameters_backward = self._transform_parameters_backward1
        else:
            self._transform_parameters_forward = self._transform_parameters_forward2
            self._transform_parameters_backward = self._transform_parameters_backward2

    def basis_predictions(self, paradigm, parameters):

        if hasattr(paradigm, 'values'):
            paradigm = paradigm.values

        if hasattr(parameters, 'values'):
            parameters = parameters.values

        paradigm = np.float32(paradigm)
        parameters = np.float32(parameters)

        if paradigm.ndim == 1:
            paradigm = paradigm[:, np.newaxis]

        return self._basis_predictions(paradigm[np.newaxis, ...], parameters[np.newaxis, ...])[0]

    def get_init_pars(self, data, paradigm, confounds=None):

        if confounds is not None:
            beta = tf.linalg.lstsq(confounds, data)
            predictions = (confounds @ beta)
            data -= predictions

        if hasattr(data, 'values'):
            data = data.values

        if hasattr(paradigm, 'values'):
            paradigm = paradigm.values

        baselines = tf.reduce_min(data, 0)
        data_ = (data - baselines)

        mus = tf.reduce_sum((data_ * paradigm), 0) / tf.reduce_sum(data_, 0)
        sds = tf.sqrt(tf.reduce_sum(data_ * (paradigm - mus)
                                    ** 2, 0) / tf.reduce_sum(data_, 0))
        amplitudes = tf.reduce_max(data_, 0)

        parameters = tf.concat([mus[:, tf.newaxis],
                                sds[:, tf.newaxis],
                                amplitudes[:, tf.newaxis],
                                baselines[:, tf.newaxis]], 1)

        return parameters

    @tf.function
    def _basis_predictions(self, paradigm, parameters):
        # paradigm: n_batches x n_timepoints x n_stimulus_features
        # parameters:: n_batches x n_voxels x n_parameters

        # norm: n_batches x n_timepoints x n_voxels

        # output: n_batches x n_timepoints x n_voxels
        return norm(paradigm[..., tf.newaxis, 0],
                    parameters[:, tf.newaxis, :, 0],
                    parameters[:, tf.newaxis, :, 1]) * \
            parameters[:, tf.newaxis, :, 2] + parameters[:, tf.newaxis, :, 3]

    def init_pseudoWWT(self, stimulus_range, parameters):

        W = self.basis_predictions(stimulus_range, parameters)

        pseudoWWT = tf.tensordot(W, W, (0, 0))
        self._pseudoWWT = tf.where(tf.math.is_nan(pseudoWWT), tf.zeros_like(pseudoWWT),
                                   pseudoWWT)
        return self._pseudoWWT

    def get_pseudoWWT(self):

        if self.weights is not None:
            return self.weights.T.dot(self.weights).values

        if hasattr(self, '_pseudoWWT'):
            return self._pseudoWWT
        else:
            raise ValueError(
                'First initialize WWT for a specific stimulus range using init_pseudoWWT!')

    @tf.function
    def _transform_parameters_forward1(self, parameters):
        return tf.concat([parameters[:, 0][:, tf.newaxis],
                          tf.math.softplus(parameters[:, 1][:, tf.newaxis]),
                          parameters[:, 2][:, tf.newaxis],
                          parameters[:, 3][:, tf.newaxis]], axis=1)

    @tf.function
    def _transform_parameters_backward1(self, parameters):
        return tf.concat([parameters[:, 0][:, tf.newaxis],
                          tfp.math.softplus_inverse(
                              parameters[:, 1][:, tf.newaxis]),
                          parameters[:, 2][:, tf.newaxis],
                          parameters[:, 3][:, tf.newaxis]], axis=1)

    @tf.function
    def _transform_parameters_forward2(self, parameters):
        return tf.concat([parameters[:, 0][:, tf.newaxis],
                          tf.math.softplus(parameters[:, 1][:, tf.newaxis]),
                          tf.math.softplus(parameters[:, 2][:, tf.newaxis]),
                          parameters[:, 3][:, tf.newaxis]], axis=1)

    @tf.function
    def _transform_parameters_backward2(self, parameters):
        return tf.concat([parameters[:, 0][:, tf.newaxis],
                          tfp.math.softplus_inverse(
                              parameters[:, 1][:, tf.newaxis]),
                          tfp.math.softplus_inverse(
                              parameters[:, 2][:, tf.newaxis]),
                          parameters[:, 3][:, tf.newaxis]], axis=1)


class GaussianPRFWithHRF(GaussianPRF, HRFEncodingModel):
    pass


class GaussianPRF2D(EncodingModel):

    parameter_labels = ['x', 'y', 'sd', 'baseline', 'amplitude']

    def __init__(self, grid_coordinates=None, paradigm=None, data=None, parameters=None,
                 weights=None, omega=None, verbosity=logging.INFO, **kwargs):

        if grid_coordinates is None:
            grid_coordinates = np.array(np.meshgrid(np.linspace(-1, 1, paradigm.shape[1]),
                                                    np.linspace(-1, 1, paradigm.shape[2]),), dtype=np.float32)

            grid_coordinates = np.swapaxes(grid_coordinates, 2, 1)
            grid_coordinates = np.reshape(
                grid_coordinates, (len(grid_coordinates), -1)).T

        self.grid_coordinates = pd.DataFrame(
            grid_coordinates, columns=['x', 'y'])
        self._grid_coordinates = self.grid_coordinates.values

        self.n_x = len(self.grid_coordinates['x'].unique())
        self.n_y = len(self.grid_coordinates['y'].unique())

        if paradigm is not None:
            paradigm = np.reshape(paradigm, (len(paradigm), -1))

        super().__init__(paradigm=paradigm, data=data, parameters=parameters,
                         weights=weights, verbosity=logging.INFO, **kwargs)

        if paradigm is not None:
            self.paradigm.columns = pd.MultiIndex.from_frame(self.grid_coordinates)

    def get_rf(self, as_frame=False, unpack=False):

        grid_coordinates = self.grid_coordinates.values
        parameters = self.parameters.values[np.newaxis, ...]

        rf = self._get_rf(grid_coordinates, parameters).numpy()[0]

        if as_frame:
            rf = pd.concat([pd.DataFrame(e,
                                         index=pd.MultiIndex.from_frame(self.grid_coordinates))
                            for e in rf],
                           keys=self.parameters.index)

            if unpack:
                rf = rf.unstack('x').sort_index(ascending=False)

        return rf

    @tf.function
    def _basis_predictions(self, paradigm, parameters):
        # paradigm: n_batches x n_timepoints x n_stimulus_features
        # parameters:: n_batches x n_voxels x n_parameters

        # norm: n_batches x n_timepoints x n_voxels

        # output: n_batches x n_timepoints x n_voxels

        rf = self._get_rf(self.grid_coordinates, parameters)
        baseline = parameters[:, tf.newaxis, :, 3]
        result = tf.tensordot(paradigm, rf, (2, 2))[:, :, 0, :] + baseline

        return result

    @tf.function
    def _get_rf(self, grid_coordinates, parameters):

        # n_batches x n_populations x  n_grid_spaces
        x = grid_coordinates[:, 0][tf.newaxis, tf.newaxis, :]
        y = grid_coordinates[:, 1][tf.newaxis, tf.newaxis, :]

        # n_batches x n_populations x n_grid_spaces (broadcast)
        mu_x = parameters[:, :, 0, tf.newaxis]
        mu_y = parameters[:, :, 1, tf.newaxis]
        sd = parameters[:, :, 2, tf.newaxis]
        amplitude = parameters[:, :, 4, tf.newaxis]

        return (tf.exp(-((x-mu_x)**2 + (y-mu_y)**2)/(2*sd**2))) * amplitude

    @tf.function
    def _transform_parameters_forward(self, parameters):
        return tf.concat([parameters[:, 0][:, tf.newaxis],
                          parameters[:, 1][:, tf.newaxis],
                          tf.math.softplus(parameters[:, 2][:, tf.newaxis]),
                          parameters[:, 3][:, tf.newaxis],
                          parameters[:, 4][:, tf.newaxis]], axis=1)

    @tf.function
    def _transform_parameters_backward(self, parameters):
        return tf.concat([parameters[:, 0][:, tf.newaxis],
                          parameters[:, 1][:, tf.newaxis],
                          tfp.math.softplus_inverse(
                              parameters[:, 2][:, tf.newaxis]),
                          parameters[:, 3][:, tf.newaxis],
                          parameters[:, 4][:, tf.newaxis]], axis=1)

    def get_pseudoWWT(self):
        rf = self.get_rf()
        return rf.dot(rf.T)

    def to_linear_model(self):
        return LinearModelWithBaseline(self.paradigm, self.data, self.parameters[['baseline']], weights=self.get_rf().T)

    def unpack_stimulus(self, stimulus):
        return np.reshape(stimulus, (-1, self.n_x, self.n_y))

class GaussianPRF2DAngle(GaussianPRF2D):

    parameter_labels = ['theta', 'ecc', 'sd', 'baseline', 'amplitude']

    @tf.function
    def _get_rf(self, grid_coordinates, parameters):

        # n_batches x n_populations x  n_grid_spaces
        x = grid_coordinates[:, 0][tf.newaxis, tf.newaxis, :]
        y = grid_coordinates[:, 1][tf.newaxis, tf.newaxis, :]

        # n_batches x n_populations x n_grid_spaces (broadcast)
        theta = parameters[:, :, 0, tf.newaxis]
        ecc = parameters[:, :, 1, tf.newaxis]
        mu_x = tf.math.cos(theta) * ecc
        mu_y = tf.math.sin(theta) * ecc
        sd = parameters[:, :, 2, tf.newaxis]
        amplitude = parameters[:, :, 4, tf.newaxis]

        return (tf.exp(-((x-mu_x)**2 + (y-mu_y)**2)/(2*sd**2))) * amplitude

    @tf.function
    def _transform_parameters_forward(self, parameters):
        return tf.concat([parameters[:, 0][:, tf.newaxis],
                          tf.math.softplus(parameters[:, 1][:, tf.newaxis]),
                          tf.math.softplus(parameters[:, 2][:, tf.newaxis]),
                          parameters[:, 3][:, tf.newaxis],
                          parameters[:, 4][:, tf.newaxis]], axis=1)

    @tf.function
    def _transform_parameters_backward(self, parameters):
        return tf.concat([restrict_radians(parameters[:, 0][:, tf.newaxis]),
                          tfp.math.softplus_inverse(
                              parameters[:, 1][:, tf.newaxis]),
                          tfp.math.softplus_inverse(
                              parameters[:, 2][:, tf.newaxis]),
                          parameters[:, 3][:, tf.newaxis],
                          parameters[:, 4][:, tf.newaxis]], axis=1)

    def get_pseudoWWT(self):
        rf = self.get_rf()
        return rf.dot(rf.T)

    def to_linear_model(self):
        return LinearModelWithBaseline(self.paradigm, self.data, self.parameters[['baseline']], weights=self.get_rf().T)

    def unpack_stimulus(self, stimulus):
        return np.reshape(stimulus, (-1, self.n_x, self.n_y))

    def to_xy_model(self):
        parameters = self.parameters.copy()
        parameters['x'] = np.cos(parameters['theta']) * parameters['ecc']
        parameters['y'] = np.sin(parameters['theta']) * parameters['ecc']
        parameters = parameters[['x', 'y', 'sd', 'baseline', 'amplitude']]

        return GaussianPRF2D(grid_coordinates=self.grid_coordinates,
                paradigm=self.paradigm, data=self.data, parameters=parameters,
                     weights=self.weights, omega=self.omega)


class GaussianPRF2DWithHRF(GaussianPRF2D, HRFEncodingModel):

    def __init__(self, grid_coordinates=None, paradigm=None, data=None, parameters=None,
                 weights=None, hrf_model=None, verbosity=logging.INFO, **kwargs):

        super().__init__(grid_coordinates, paradigm, data, parameters, weights, verbosity,
                         hrf_model=hrf_model, **kwargs)

    def to_linear_model(self):
        return LinearModelWithBaselineHRF(self.paradigm, self.data,
                                          self.parameters[[
                                              'baseline']], weights=self.get_rf().T,
                                          hrf_model=self.hrf_model)

class GaussianPRF2DAngleWithHRF(GaussianPRF2DAngle, HRFEncodingModel):

    def __init__(self, grid_coordinates=None, paradigm=None, data=None, parameters=None,
                 weights=None, hrf_model=None, verbosity=logging.INFO, **kwargs):

        super().__init__(grid_coordinates, paradigm, data, parameters, weights, verbosity,
                         hrf_model=hrf_model, **kwargs)

    def to_linear_model(self):
        return LinearModelWithBaselineHRF(self.paradigm, self.data,
                                          self.parameters[[
                                              'baseline']], weights=self.get_rf().T,
                                          hrf_model=self.hrf_model)

    def to_xy_model(self):

        no_hrf_model = super().to_xy_model()

        return GaussianPRF2DWithHRF(grid_coordinates=self.grid_coordinates,
                paradigm=self.paradigm, data=self.data, parameters=no_hrf_model.parameters,
                     weights=self.weights, omega=self.omega,
                     hrf_model=self.hrf_model)

class DifferenceOfGaussiansPRF2D(GaussianPRF2D):

    # Amplitude is as a fraction of the positive amplitude and is limited to be within [0, 1]
    # srf factor is limited to be above 1
    parameter_labels = ['x', 'y', 'sd', 'baseline',
                        'amplitude', 'srf_amplitude', 'srf_factor']

    @tf.function
    def _transform_parameters_forward(self, parameters):
        return tf.concat([parameters[:, 0][:, tf.newaxis],
                          parameters[:, 1][:, tf.newaxis],
                          tf.math.softplus(parameters[:, 2][:, tf.newaxis]),
                          parameters[:, 3][:, tf.newaxis],
                          parameters[:, 4][:, tf.newaxis],
                          tf.math.softplus(parameters[:, 5][:, tf.newaxis]),
                          tf.math.softplus(parameters[:, 6][:, tf.newaxis]) + 1], axis=1)

    @tf.function
    def _transform_parameters_backward(self, parameters):
        return tf.concat([parameters[:, 0][:, tf.newaxis],
                          parameters[:, 1][:, tf.newaxis],
                          tfp.math.softplus_inverse(
                              parameters[:, 2][:, tf.newaxis]),
                          parameters[:, 3][:, tf.newaxis],
                          parameters[:, 4][:, tf.newaxis],
                          tfp.math.softplus_inverse(
                              parameters[:, 5][:, tf.newaxis]),
                          tfp.math.softplus_inverse(parameters[:, 6][:, tf.newaxis] - 1)], axis=1)

    @tf.function
    def _get_rf(self, grid_coordinates, parameters):

        # n_batches x n_populations x  n_grid_spaces
        x = grid_coordinates[:, 0][tf.newaxis, tf.newaxis, :]
        y = grid_coordinates[:, 1][tf.newaxis, tf.newaxis, :]

        # n_batches x n_populations x n_grid_spaces (broadcast)
        mu_x = parameters[:, :, 0, tf.newaxis]
        mu_y = parameters[:, :, 1, tf.newaxis]
        sd = parameters[:, :, 2, tf.newaxis]
        amplitude = parameters[:, :, 4, tf.newaxis]

        srf_amplitude = parameters[:, :, 5, tf.newaxis]
        srf_size = parameters[:, :, 6, tf.newaxis]

        standard_prf = super()._get_rf(grid_coordinates, parameters)

        srf = tf.exp(-((x-mu_x)**2 + (y-mu_y)**2)/(2*(srf_size*sd)**2)
                     ) * amplitude / srf_size**2

        return standard_prf - srf_amplitude * srf


class DifferenceOfGaussiansPRF2DWithHRF(DifferenceOfGaussiansPRF2D, HRFEncodingModel):

    def __init__(self, grid_coordinates=None, paradigm=None, data=None, parameters=None,
                 weights=None, hrf_model=None, verbosity=logging.INFO):

        super().__init__(grid_coordinates, paradigm, data, parameters, weights, verbosity,
                         hrf_model=hrf_model)


class DiscreteModel(EncodingModel):

    def __init__(self, paradigm=None, data=None, parameters=None,
                 weights=None, verbosity=logging.INFO):

        self.parameter_labels = ['stim=={}'.format(
            p) for p in np.diag(parameters)]
        _parameters = np.zeros_like(parameters) * np.nan
        _parameters[np.diag_indices(len(parameters))] = np.diag(parameters)

        super().__init__(paradigm, data, _parameters, weights, verbosity)

    @tf.function
    def _basis_predictions(self, paradigm, parameters):

        parameters_ = tf.linalg.diag_part(parameters)

        return tf.cast(tf.equal(paradigm, parameters_[tf.newaxis, :]), tf.float32)


class LinearModel(EncodingModel):

    def __init__(self, paradigm=None, data=None, parameters=None,
                 weights=None, omega=None, verbosity=logging.INFO, **kwargs):

        if parameters is not None:
            raise ValueError('LinearModel does not use any parameters!')

        super().__init__(paradigm=paradigm, data=data, parameters=parameters,
                         weights=weights, verbosity=logging.INFO, **kwargs)

    def predict(self, paradigm=None, parameters=None, weights=None):

        if parameters is not None:
            raise ValueError('LinearModel does not use any parameters!')

        return super().predict(paradigm, parameters, weights)

    @tf.function
    def _basis_predictions(self, paradigm, parameters):
        return paradigm


class LinearModelWithBaseline(EncodingModel):
    @tf.function
    def _predict(self, paradigm, parameters, weights=None):

        basis_predictions = self._basis_predictions(paradigm, None)

        if weights is None:
            return basis_predictions + parameters[..., 0]
        else:
            return tf.tensordot(basis_predictions, weights, (2, 1))[:, :, 0, :] + \
                tf.transpose(parameters, [0, 2, 1])

    @tf.function
    def _basis_predictions(self, paradigm, parameters):
        return paradigm


class LinearModelWithBaselineHRF(LinearModelWithBaseline, HRFEncodingModel):

    def __init__(self, paradigm=None, data=None, parameters=None,
                 weights=None, hrf_model=None, verbosity=logging.INFO,
                 **kwargs):

        super().__init__(paradigm=paradigm,
                         data=data,
                         parameters=parameters,
                         weights=weights,
                         verbosity=verbosity,
                         hrf_model=hrf_model,
                         **kwargs)

    @tf.function
    def _predict(self, paradigm, parameters, weights):
        pre_convolve = LinearModelWithBaseline._predict(
            self, paradigm, parameters, weights)

        return self.hrf_model.convolve(pre_convolve)

    @tf.function
    def _predict_no_hrf(self, paradigm, parameters, weights):
        return LinearModelWithBaseline._predict(self, paradigm, parameters, weights)
