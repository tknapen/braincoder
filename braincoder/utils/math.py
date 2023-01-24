import tensorflow as tf
import pandas as pd
import numpy as np


def norm(x, mu, sigma):
    kernel = tf.math.exp(-.5 * (x - mu)**2. / sigma**2)
    return kernel


def logit(x):
    """ Computes the logit function, i.e. the logistic sigmoid inverse. """
    return tf.clip_by_value(-tf.math.log(1. / x - 1.), -1e12, 1e12)


@tf.function
def log2(x):
    return tf.math.log(x) / tf.math.log(2.)

@tf.function
def restrict_radians(x):
    x = x+np.pi
    return x - tf.floor(x / (2*np.pi)) * 2*np.pi - np.pi

def lognormalpdf_n(x, mu_n, sigma_n, normalize=False):

    denom = 1+sigma_n**2/mu_n**2

    part2 = tf.exp(-((tf.math.log(x)- tf.math.log(mu_n / tf.sqrt(denom)))**2 / (2*tf.math.log(denom))))

    if normalize:
        part1 = 1. / (x*tf.sqrt(2*np.pi*tf.math.log(denom)))
        return part1*part2
    else:
        return part2