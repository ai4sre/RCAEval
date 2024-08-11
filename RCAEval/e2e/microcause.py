# take from https://github.com/PanYicheng/dycause_rca/tree/main
# with update
from math import floor, log

import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import pandas as pd
import pingouin as pg
import tigramite.data_processing as pp
import tqdm
from causallearn.search.ConstraintBased.PC import pc
from pingouin import partial_corr
from scipy.optimize import minimize
from tigramite.independence_tests.parcorr import ParCorr
from tigramite.pcmci import PCMCI

from cfm.classes.graph import Graph, MemoryGraph, Node
from cfm.graph_construction.pc import pc_default
from cfm.graph_construction.pcmci import pcmci
from cfm.graph_heads import finalize_directed_adj
from cfm.io.time_series import drop_constant, drop_extra, drop_near_constant, drop_time, preprocess

# colors for plot
deep_saffron = "#FF9933"
air_force_blue = "#5D8AA8"


"""
================================= MAIN CLASS ==================================
"""


class SPOT:
    """
    This class allows to run SPOT algorithm on univariate dataset (upper-bound)

    Attributes
    ----------
    proba : float
        Detection level (risk), chosen by the user

    extreme_quantile : float
        current threshold (bound between normal and abnormal events)

    data : numpy.array
        stream

    init_data : numpy.array
        initial batch of observations (for the calibration/initialization step)

    init_threshold : float
        initial threshold computed during the calibration step

    peaks : numpy.array
        array of peaks (excesses above the initial threshold)

    n : int
        number of observed values

    Nt : int
        number of observed peaks
    """

    def __init__(self, q=1e-4):
        """
        Constructor

            Parameters
            ----------
            q
                    Detection level (risk)

            Returns
            ----------
        SPOT object
        """
        self.proba = q
        self.extreme_quantile = None
        self.data = None
        self.init_data = None
        self.init_threshold = None
        self.peaks = None
        self.n = 0
        self.Nt = 0

    def __str__(self):
        s = ""
        s += "Streaming Peaks-Over-Threshold Object\n"
        s += "Detection level q = %s\n" % self.proba
        if self.data is not None:
            s += "Data imported : Yes\n"
            s += "\t initialization  : %s values\n" % self.init_data.size
            s += "\t stream : %s values\n" % self.data.size
        else:
            s += "Data imported : No\n"
            return s

        if self.n == 0:
            s += "Algorithm initialized : No\n"
        else:
            s += "Algorithm initialized : Yes\n"
            s += "\t initial threshold : %s\n" % self.init_threshold

            r = self.n - self.init_data.size
            if r > 0:
                s += "Algorithm run : Yes\n"
                s += "\t number of observations : %s (%.2f %%)\n" % (r, 100 * r / self.n)
            else:
                s += "\t number of peaks  : %s\n" % self.Nt
                s += "\t extreme quantile : %s\n" % self.extreme_quantile
                s += "Algorithm run : No\n"
        return s

    def fit(self, init_data, data):
        """
        Import data to SPOT object

        Parameters
            ----------
            init_data : list, numpy.array or pandas.Series
                    initial batch to calibrate the algorithm

        data : numpy.array
                    data for the run (list, np.array or pd.series)

        """
        if isinstance(data, list):
            self.data = np.array(data)
        elif isinstance(data, np.ndarray):
            self.data = data
        elif isinstance(data, pd.Series):
            self.data = data.values
        else:
            print("This data format (%s) is not supported" % type(data))
            return

        if isinstance(init_data, list):
            self.init_data = np.array(init_data)
        elif isinstance(init_data, np.ndarray):
            self.init_data = init_data
        elif isinstance(init_data, pd.Series):
            self.init_data = init_data.values
        elif isinstance(init_data, int):
            self.init_data = self.data[:init_data]
            self.data = self.data[init_data:]
        elif isinstance(init_data, float) & (init_data < 1) & (init_data > 0):
            r = int(init_data * data.size)
            self.init_data = self.data[:r]
            self.data = self.data[r:]
        else:
            print("The initial data cannot be set")
            return

    def add(self, data):
        """
        This function allows to append data to the already fitted data

        Parameters
            ----------
            data : list, numpy.array, pandas.Series
                    data to append
        """
        if isinstance(data, list):
            data = np.array(data)
        elif isinstance(data, np.ndarray):
            data = data
        elif isinstance(data, pd.Series):
            data = data.values
        else:
            print("This data format (%s) is not supported" % type(data))
            return

        self.data = np.append(self.data, data)
        return

    def initialize(self, level=0.98, verbose=True):
        """
        Run the calibration (initialization) step

        Parameters
            ----------
        level : float
            (default 0.98) Probability associated with the initial threshold t
            verbose : bool
                    (default = True) If True, gives details about the batch initialization
        """
        level = level - floor(level)

        n_init = self.init_data.size

        S = np.sort(self.init_data)  # we sort X to get the empirical quantile
        self.init_threshold = S[int(level * n_init)]  # t is fixed for the whole algorithm

        # initial peaks
        self.peaks = self.init_data[self.init_data > self.init_threshold] - self.init_threshold
        self.Nt = self.peaks.size
        self.n = n_init

        if verbose:
            print("Initial threshold : %s" % self.init_threshold)
            print("Number of peaks : %s" % self.Nt)
            print("Grimshaw maximum log-likelihood estimation ... ", end="")

        g, s, l = self._grimshaw()
        self.extreme_quantile = self._quantile(g, s)

        if verbose:
            print("[done]")
            print("\t" + chr(0x03B3) + " = " + str(g))
            print("\t" + chr(0x03C3) + " = " + str(s))
            print("\tL = " + str(l))
            print("Extreme quantile (probability = %s): %s" % (self.proba, self.extreme_quantile))

        return

    def _rootsFinder(fun, jac, bounds, npoints, method):
        """
        Find possible roots of a scalar function

        Parameters
        ----------
        fun : function
                    scalar function
        jac : function
            first order derivative of the function
        bounds : tuple
            (min,max) interval for the roots search
        npoints : int
            maximum number of roots to output
        method : str
            'regular' : regular sample of the search interval, 'random' : uniform (distribution) sample of the search interval

        Returns
        ----------
        numpy.array
            possible roots of the function
        """
        if method == "regular":
            step = (bounds[1] - bounds[0]) / (npoints + 1)
            X0 = np.arange(bounds[0] + step, bounds[1], step)
        elif method == "random":
            X0 = np.random.uniform(bounds[0], bounds[1], npoints)

        def objFun(X, f, jac):
            g = 0
            j = np.zeros(X.shape)
            i = 0
            for x in X:
                fx = f(x)
                g = g + fx**2
                j[i] = 2 * fx * jac(x)
                i = i + 1
            return g, j

        opt = minimize(
            lambda X: objFun(X, fun, jac),
            X0,
            method="L-BFGS-B",
            jac=True,
            bounds=[bounds] * len(X0),
        )

        X = opt.x
        np.round(X, decimals=5)
        return np.unique(X)

    def _log_likelihood(Y, gamma, sigma):
        """
        Compute the log-likelihood for the Generalized Pareto Distribution (μ=0)

        Parameters
        ----------
        Y : numpy.array
                    observations
        gamma : float
            GPD index parameter
        sigma : float
            GPD scale parameter (>0)

        Returns
        ----------
        float
            log-likelihood of the sample Y to be drawn from a GPD(γ,σ,μ=0)
        """
        n = Y.size
        if gamma != 0:
            tau = gamma / sigma
            L = -n * log(sigma) - (1 + (1 / gamma)) * (np.log(1 + tau * Y)).sum()
        else:
            L = n * (1 + log(Y.mean()))
        return L

    def _grimshaw(self, epsilon=1e-8, n_points=10):
        """
        Compute the GPD parameters estimation with the Grimshaw's trick

        Parameters
        ----------
        epsilon : float
                    numerical parameter to perform (default : 1e-8)
        n_points : int
            maximum number of candidates for maximum likelihood (default : 10)

        Returns
        ----------
        gamma_best,sigma_best,ll_best
            gamma estimates, sigma estimates and corresponding log-likelihood
        """

        def u(s):
            return 1 + np.log(s).mean()

        def v(s):
            return np.mean(1 / s)

        def w(Y, t):
            s = 1 + t * Y
            us = u(s)
            vs = v(s)
            return us * vs - 1

        def jac_w(Y, t):
            s = 1 + t * Y
            us = u(s)
            vs = v(s)
            jac_us = (1 / t) * (1 - vs)
            jac_vs = (1 / t) * (-vs + np.mean(1 / s**2))
            return us * jac_vs + vs * jac_us

        Ym = self.peaks.min()
        YM = self.peaks.max()
        Ymean = self.peaks.mean()

        a = -1 / YM
        if abs(a) < 2 * epsilon:
            epsilon = abs(a) / n_points

        a = a + epsilon
        b = 2 * (Ymean - Ym) / (Ymean * Ym)
        c = 2 * (Ymean - Ym) / (Ym**2)

        # We look for possible roots
        left_zeros = SPOT._rootsFinder(
            lambda t: w(self.peaks, t),
            lambda t: jac_w(self.peaks, t),
            (a + epsilon, -epsilon),
            n_points,
            "regular",
        )

        right_zeros = SPOT._rootsFinder(
            lambda t: w(self.peaks, t), lambda t: jac_w(self.peaks, t), (b, c), n_points, "regular"
        )

        # all the possible roots
        zeros = np.concatenate((left_zeros, right_zeros))

        # 0 is always a solution so we initialize with it
        gamma_best = 0
        sigma_best = Ymean
        ll_best = SPOT._log_likelihood(self.peaks, gamma_best, sigma_best)

        # we look for better candidates
        for z in zeros:
            gamma = u(1 + z * self.peaks) - 1
            sigma = gamma / z
            ll = SPOT._log_likelihood(self.peaks, gamma, sigma)
            if ll > ll_best:
                gamma_best = gamma
                sigma_best = sigma
                ll_best = ll

        return gamma_best, sigma_best, ll_best

    def _quantile(self, gamma, sigma):
        """
        Compute the quantile at level 1-q

        Parameters
        ----------
        gamma : float
                    GPD parameter
        sigma : float
            GPD parameter

        Returns
        ----------
        float
            quantile at level 1-q for the GPD(γ,σ,μ=0)
        """
        r = self.n * self.proba / self.Nt
        if gamma != 0:
            return self.init_threshold + (sigma / gamma) * (pow(r, -gamma) - 1)
        else:
            return self.init_threshold - sigma * log(r)

    def run(self, with_alarm=True):
        """
        Run SPOT on the stream

        Parameters
        ----------
        with_alarm : bool
		    (default = True) If False, SPOT will adapt the threshold assuming \
            there is no abnormal values


        Returns
        ----------
        dict
            keys : 'thresholds' and 'alarms'

            'thresholds' contains the extreme quantiles and 'alarms' contains \
            the indexes of the values which have triggered alarms

        """
        if self.n > self.init_data.size:
            print(
                "Warning : the algorithm seems to have already been run, you \
            should initialize before running again"
            )
            return {}

        # list of the thresholds
        th = []
        alarm = []
        # Loop over the stream
        for i in tqdm.tqdm(range(self.data.size), ascii=True):
            # If the observed value exceeds the current threshold (alarm case)
            if self.data[i] > self.extreme_quantile:
                # if we want to alarm, we put it in the alarm list
                if with_alarm:
                    alarm.append(i)
                # otherwise we add it in the peaks
                else:
                    self.peaks = np.append(self.peaks, self.data[i] - self.init_threshold)
                    self.Nt += 1
                    self.n += 1
                    # and we update the thresholds

                    g, s, l = self._grimshaw()
                    self.extreme_quantile = self._quantile(g, s)

            # case where the value exceeds the initial threshold but not the alarm ones
            elif self.data[i] > self.init_threshold:
                # we add it in the peaks
                self.peaks = np.append(self.peaks, self.data[i] - self.init_threshold)
                self.Nt += 1
                self.n += 1
                # and we update the thresholds

                g, s, l = self._grimshaw()
                self.extreme_quantile = self._quantile(g, s)
            else:
                self.n += 1

            th.append(self.extreme_quantile)  # thresholds record

        return {"thresholds": th, "alarms": alarm}

    def plot(self, run_results, with_alarm=True):
        """
        Plot the results of given by the run

        Parameters
        ----------
        run_results : dict
            results given by the 'run' method
        with_alarm : bool
                    (default = True) If True, alarms are plotted.


        Returns
        ----------
        list
            list of the plots

        """
        x = range(self.data.size)
        K = run_results.keys()

        (ts_fig,) = plt.plot(x, self.data, color=air_force_blue)
        fig = [ts_fig]

        if "thresholds" in K:
            th = run_results["thresholds"]
            (th_fig,) = plt.plot(x, th, color=deep_saffron, lw=2, ls="dashed")
            fig.append(th_fig)

        if with_alarm and ("alarms" in K):
            alarm = run_results["alarms"]
            al_fig = plt.scatter(alarm, self.data[alarm], color="red")
            fig.append(al_fig)

        plt.xlim((0, self.data.size))

        return fig


"""
============================ UPPER & LOWER BOUNDS =============================
"""


class biSPOT:
    """
    This class allows to run biSPOT algorithm on univariate dataset (upper and lower bounds)

    Attributes
    ----------
    proba : float
        Detection level (risk), chosen by the user

    extreme_quantile : float
        current threshold (bound between normal and abnormal events)

    data : numpy.array
        stream

    init_data : numpy.array
        initial batch of observations (for the calibration/initialization step)

    init_threshold : float
        initial threshold computed during the calibration step

    peaks : numpy.array
        array of peaks (excesses above the initial threshold)

    n : int
        number of observed values

    Nt : int
        number of observed peaks
    """

    def __init__(self, q=1e-4):
        """
        Constructor

            Parameters
            ----------
            q
                    Detection level (risk)

            Returns
            ----------
        biSPOT object
        """
        self.proba = q
        self.data = None
        self.init_data = None
        self.n = 0
        nonedict = {"up": None, "down": None}

        self.extreme_quantile = dict.copy(nonedict)
        self.init_threshold = dict.copy(nonedict)
        self.peaks = dict.copy(nonedict)
        self.gamma = dict.copy(nonedict)
        self.sigma = dict.copy(nonedict)
        self.Nt = {"up": 0, "down": 0}

    def __str__(self):
        s = ""
        s += "Streaming Peaks-Over-Threshold Object\n"
        s += "Detection level q = %s\n" % self.proba
        if self.data is not None:
            s += "Data imported : Yes\n"
            s += "\t initialization  : %s values\n" % self.init_data.size
            s += "\t stream : %s values\n" % self.data.size
        else:
            s += "Data imported : No\n"
            return s

        if self.n == 0:
            s += "Algorithm initialized : No\n"
        else:
            s += "Algorithm initialized : Yes\n"
            s += "\t initial threshold : %s\n" % self.init_threshold

            r = self.n - self.init_data.size
            if r > 0:
                s += "Algorithm run : Yes\n"
                s += "\t number of observations : %s (%.2f %%)\n" % (r, 100 * r / self.n)
                s += "\t triggered alarms : %s (%.2f %%)\n" % (
                    len(self.alarm),
                    100 * len(self.alarm) / self.n,
                )
            else:
                s += "\t number of peaks  : %s\n" % self.Nt
                s += "\t upper extreme quantile : %s\n" % self.extreme_quantile["up"]
                s += "\t lower extreme quantile : %s\n" % self.extreme_quantile["down"]
                s += "Algorithm run : No\n"
        return s

    def fit(self, init_data, data):
        """
        Import data to biSPOT object

        Parameters
            ----------
            init_data : list, numpy.array or pandas.Series
                    initial batch to calibrate the algorithm ()

        data : numpy.array
                    data for the run (list, np.array or pd.series)

        """
        if isinstance(data, list):
            self.data = np.array(data)
        elif isinstance(data, np.ndarray):
            self.data = data
        elif isinstance(data, pd.Series):
            self.data = data.values
        else:
            print("This data format (%s) is not supported" % type(data))
            return

        if isinstance(init_data, list):
            self.init_data = np.array(init_data)
        elif isinstance(init_data, np.ndarray):
            self.init_data = init_data
        elif isinstance(init_data, pd.Series):
            self.init_data = init_data.values
        elif isinstance(init_data, int):
            self.init_data = self.data[:init_data]
            self.data = self.data[init_data:]
        elif isinstance(init_data, float) & (init_data < 1) & (init_data > 0):
            r = int(init_data * data.size)
            self.init_data = self.data[:r]
            self.data = self.data[r:]
        else:
            print("The initial data cannot be set")
            return

    def add(self, data):
        """
        This function allows to append data to the already fitted data

        Parameters
            ----------
            data : list, numpy.array, pandas.Series
                    data to append
        """
        if isinstance(data, list):
            data = np.array(data)
        elif isinstance(data, np.ndarray):
            data = data
        elif isinstance(data, pd.Series):
            data = data.values
        else:
            print("This data format (%s) is not supported" % type(data))
            return

        self.data = np.append(self.data, data)
        return

    def initialize(self, verbose=True):
        """
        Run the calibration (initialization) step

        Parameters
            ----------
            verbose : bool
                    (default = True) If True, gives details about the batch initialization
        """
        n_init = self.init_data.size

        S = np.sort(self.init_data)  # we sort X to get the empirical quantile
        self.init_threshold["up"] = S[int(0.98 * n_init)]  # t is fixed for the whole algorithm
        self.init_threshold["down"] = S[int(0.02 * n_init)]  # t is fixed for the whole algorithm

        # initial peaks
        self.peaks["up"] = (
            self.init_data[self.init_data > self.init_threshold["up"]] - self.init_threshold["up"]
        )
        self.peaks["down"] = -(
            self.init_data[self.init_data < self.init_threshold["down"]]
            - self.init_threshold["down"]
        )
        self.Nt["up"] = self.peaks["up"].size
        self.Nt["down"] = self.peaks["down"].size
        self.n = n_init

        if verbose:
            print("Initial threshold : %s" % self.init_threshold)
            print("Number of peaks : %s" % self.Nt)
            print("Grimshaw maximum log-likelihood estimation ... ", end="")

        l = {"up": None, "down": None}
        for side in ["up", "down"]:
            g, s, l[side] = self._grimshaw(side)
            self.extreme_quantile[side] = self._quantile(side, g, s)
            self.gamma[side] = g
            self.sigma[side] = s

        ltab = 20
        form = "\t" + "%20s" + "%20.2f" + "%20.2f"
        if verbose:
            print("[done]")
            print("\t" + "Parameters".rjust(ltab) + "Upper".rjust(ltab) + "Lower".rjust(ltab))
            print("\t" + "-" * ltab * 3)
            print(form % (chr(0x03B3), self.gamma["up"], self.gamma["down"]))
            print(form % (chr(0x03C3), self.sigma["up"], self.sigma["down"]))
            print(form % ("likelihood", l["up"], l["down"]))
            print(
                form
                % ("Extreme quantile", self.extreme_quantile["up"], self.extreme_quantile["down"])
            )
            print("\t" + "-" * ltab * 3)
        return

    def _rootsFinder(fun, jac, bounds, npoints, method):
        """
        Find possible roots of a scalar function

        Parameters
        ----------
        fun : function
                    scalar function
        jac : function
            first order derivative of the function
        bounds : tuple
            (min,max) interval for the roots search
        npoints : int
            maximum number of roots to output
        method : str
            'regular' : regular sample of the search interval, 'random' : uniform (distribution) sample of the search interval

        Returns
        ----------
        numpy.array
            possible roots of the function
        """
        if method == "regular":
            step = (bounds[1] - bounds[0]) / (npoints + 1)
            X0 = np.arange(bounds[0] + step, bounds[1], step)
        elif method == "random":
            X0 = np.random.uniform(bounds[0], bounds[1], npoints)

        def objFun(X, f, jac):
            g = 0
            j = np.zeros(X.shape)
            i = 0
            for x in X:
                fx = f(x)
                g = g + fx**2
                j[i] = 2 * fx * jac(x)
                i = i + 1
            return g, j

        opt = minimize(
            lambda X: objFun(X, fun, jac),
            X0,
            method="L-BFGS-B",
            jac=True,
            bounds=[bounds] * len(X0),
        )

        X = opt.x
        np.round(X, decimals=5)
        return np.unique(X)

    def _log_likelihood(Y, gamma, sigma):
        """
        Compute the log-likelihood for the Generalized Pareto Distribution (μ=0)

        Parameters
        ----------
        Y : numpy.array
                    observations
        gamma : float
            GPD index parameter
        sigma : float
            GPD scale parameter (>0)

        Returns
        ----------
        float
            log-likelihood of the sample Y to be drawn from a GPD(γ,σ,μ=0)
        """
        n = Y.size
        if gamma != 0:
            tau = gamma / sigma
            L = -n * log(sigma) - (1 + (1 / gamma)) * (np.log(1 + tau * Y)).sum()
        else:
            L = n * (1 + log(Y.mean()))
        return L

    def _grimshaw(self, side, epsilon=1e-8, n_points=10):
        """
        Compute the GPD parameters estimation with the Grimshaw's trick

        Parameters
        ----------
        epsilon : float
                    numerical parameter to perform (default : 1e-8)
        n_points : int
            maximum number of candidates for maximum likelihood (default : 10)

        Returns
        ----------
        gamma_best,sigma_best,ll_best
            gamma estimates, sigma estimates and corresponding log-likelihood
        """

        def u(s):
            return 1 + np.log(s).mean()

        def v(s):
            return np.mean(1 / s)

        def w(Y, t):
            s = 1 + t * Y
            us = u(s)
            vs = v(s)
            return us * vs - 1

        def jac_w(Y, t):
            s = 1 + t * Y
            us = u(s)
            vs = v(s)
            jac_us = (1 / t) * (1 - vs)
            jac_vs = (1 / t) * (-vs + np.mean(1 / s**2))
            return us * jac_vs + vs * jac_us

        Ym = self.peaks[side].min()
        YM = self.peaks[side].max()
        Ymean = self.peaks[side].mean()

        a = -1 / YM
        if abs(a) < 2 * epsilon:
            epsilon = abs(a) / n_points

        a = a + epsilon
        b = 2 * (Ymean - Ym) / (Ymean * Ym)
        c = 2 * (Ymean - Ym) / (Ym**2)

        # We look for possible roots
        left_zeros = biSPOT._rootsFinder(
            lambda t: w(self.peaks[side], t),
            lambda t: jac_w(self.peaks[side], t),
            (a + epsilon, -epsilon),
            n_points,
            "regular",
        )

        right_zeros = biSPOT._rootsFinder(
            lambda t: w(self.peaks[side], t),
            lambda t: jac_w(self.peaks[side], t),
            (b, c),
            n_points,
            "regular",
        )

        # all the possible roots
        zeros = np.concatenate((left_zeros, right_zeros))

        # 0 is always a solution so we initialize with it
        gamma_best = 0
        sigma_best = Ymean
        ll_best = biSPOT._log_likelihood(self.peaks[side], gamma_best, sigma_best)

        # we look for better candidates
        for z in zeros:
            gamma = u(1 + z * self.peaks[side]) - 1
            sigma = gamma / z
            ll = biSPOT._log_likelihood(self.peaks[side], gamma, sigma)
            if ll > ll_best:
                gamma_best = gamma
                sigma_best = sigma
                ll_best = ll

        return gamma_best, sigma_best, ll_best

    def _quantile(self, side, gamma, sigma):
        """
        Compute the quantile at level 1-q for a given side

        Parameters
        ----------
        side : str
            'up' or 'down'
        gamma : float
                    GPD parameter
        sigma : float
            GPD parameter

        Returns
        ----------
        float
            quantile at level 1-q for the GPD(γ,σ,μ=0)
        """
        if side == "up":
            r = self.n * self.proba / self.Nt[side]
            if gamma != 0:
                return self.init_threshold["up"] + (sigma / gamma) * (pow(r, -gamma) - 1)
            else:
                return self.init_threshold["up"] - sigma * log(r)
        elif side == "down":
            r = self.n * self.proba / self.Nt[side]
            if gamma != 0:
                return self.init_threshold["down"] - (sigma / gamma) * (pow(r, -gamma) - 1)
            else:
                return self.init_threshold["down"] + sigma * log(r)
        else:
            print("error : the side is not right")

    def run(self, with_alarm=True):
        """
        Run biSPOT on the stream

        Parameters
        ----------
        with_alarm : bool
		    (default = True) If False, SPOT will adapt the threshold assuming \
            there is no abnormal values


        Returns
        ----------
        dict
            keys : 'upper_thresholds', 'lower_thresholds' and 'alarms'

            '***-thresholds' contains the extreme quantiles and 'alarms' contains \
            the indexes of the values which have triggered alarms

        """
        if self.n > self.init_data.size:
            print(
                "Warning : the algorithm seems to have already been run, you \
            should initialize before running again"
            )
            return {}

        # list of the thresholds
        thup = []
        thdown = []
        alarm = []
        # Loop over the stream
        for i in tqdm.tqdm(range(self.data.size), ascii=True):
            # If the observed value exceeds the current threshold (alarm case)
            if self.data[i] > self.extreme_quantile["up"]:
                # if we want to alarm, we put it in the alarm list
                if with_alarm:
                    alarm.append(i)
                # otherwise we add it in the peaks
                else:
                    self.peaks["up"] = np.append(
                        self.peaks["up"], self.data[i] - self.init_threshold["up"]
                    )
                    self.Nt["up"] += 1
                    self.n += 1
                    # and we update the thresholds

                    g, s, l = self._grimshaw("up")
                    self.extreme_quantile["up"] = self._quantile("up", g, s)

            # case where the value exceeds the initial threshold but not the alarm ones
            elif self.data[i] > self.init_threshold["up"]:
                # we add it in the peaks
                self.peaks["up"] = np.append(
                    self.peaks["up"], self.data[i] - self.init_threshold["up"]
                )
                self.Nt["up"] += 1
                self.n += 1
                # and we update the thresholds

                g, s, l = self._grimshaw("up")
                self.extreme_quantile["up"] = self._quantile("up", g, s)

            elif self.data[i] < self.extreme_quantile["down"]:
                # if we want to alarm, we put it in the alarm list
                if with_alarm:
                    alarm.append(i)
                # otherwise we add it in the peaks
                else:
                    self.peaks["down"] = np.append(
                        self.peaks["down"], -(self.data[i] - self.init_threshold["down"])
                    )
                    self.Nt["down"] += 1
                    self.n += 1
                    # and we update the thresholds

                    g, s, l = self._grimshaw("down")
                    self.extreme_quantile["down"] = self._quantile("down", g, s)

            # case where the value exceeds the initial threshold but not the alarm ones
            elif self.data[i] < self.init_threshold["down"]:
                # we add it in the peaks
                self.peaks["down"] = np.append(
                    self.peaks["down"], -(self.data[i] - self.init_threshold["down"])
                )
                self.Nt["down"] += 1
                self.n += 1
                # and we update the thresholds

                g, s, l = self._grimshaw("down")
                self.extreme_quantile["down"] = self._quantile("down", g, s)
            else:
                self.n += 1

            thup.append(self.extreme_quantile["up"])  # thresholds record
            thdown.append(self.extreme_quantile["down"])  # thresholds record

        return {"upper_thresholds": thup, "lower_thresholds": thdown, "alarms": alarm}

    def plot(self, run_results, with_alarm=True):
        """
        Plot the results of given by the run

        Parameters
        ----------
        run_results : dict
            results given by the 'run' method
        with_alarm : bool
                    (default = True) If True, alarms are plotted.


        Returns
        ----------
        list
            list of the plots

        """
        x = range(self.data.size)
        K = run_results.keys()

        (ts_fig,) = plt.plot(x, self.data, color=air_force_blue)
        fig = [ts_fig]

        if "upper_thresholds" in K:
            thup = run_results["upper_thresholds"]
            (uth_fig,) = plt.plot(x, thup, color=deep_saffron, lw=2, ls="dashed")
            fig.append(uth_fig)

        if "lower_thresholds" in K:
            thdown = run_results["lower_thresholds"]
            (lth_fig,) = plt.plot(x, thdown, color=deep_saffron, lw=2, ls="dashed")
            fig.append(lth_fig)

        if with_alarm and ("alarms" in K):
            alarm = run_results["alarms"]
            al_fig = plt.scatter(alarm, self.data[alarm], color="red")
            fig.append(al_fig)

        plt.xlim((0, self.data.size))

        return fig


"""
================================= WITH DRIFT ==================================
"""


def backMean(X, d):
    M = []
    w = X[:d].sum()
    M.append(w / d)
    for i in range(d, len(X)):
        w = w - X[i - d] + X[i]
        M.append(w / d)
    return np.array(M)


class dSPOT:
    """
    This class allows to run DSPOT algorithm on univariate dataset (upper-bound)

    Attributes
    ----------
    proba : float
        Detection level (risk), chosen by the user

    depth : int
        Number of observations to compute the moving average

    extreme_quantile : float
        current threshold (bound between normal and abnormal events)

    data : numpy.array
        stream

    init_data : numpy.array
        initial batch of observations (for the calibration/initialization step)

    init_threshold : float
        initial threshold computed during the calibration step

    peaks : numpy.array
        array of peaks (excesses above the initial threshold)

    n : int
        number of observed values

    Nt : int
        number of observed peaks
    """

    def __init__(self, q, depth):
        self.proba = q
        self.extreme_quantile = None
        self.data = None
        self.init_data = None
        self.init_threshold = None
        self.peaks = None
        self.n = 0
        self.Nt = 0
        self.depth = depth

    def __str__(self):
        s = ""
        s += "Streaming Peaks-Over-Threshold Object\n"
        s += "Detection level q = %s\n" % self.proba
        if self.data is not None:
            s += "Data imported : Yes\n"
            s += "\t initialization  : %s values\n" % self.init_data.size
            s += "\t stream : %s values\n" % self.data.size
        else:
            s += "Data imported : No\n"
            return s

        if self.n == 0:
            s += "Algorithm initialized : No\n"
        else:
            s += "Algorithm initialized : Yes\n"
            s += "\t initial threshold : %s\n" % self.init_threshold

            r = self.n - self.init_data.size
            if r > 0:
                s += "Algorithm run : Yes\n"
                s += "\t number of observations : %s (%.2f %%)\n" % (r, 100 * r / self.n)
                s += "\t triggered alarms : %s (%.2f %%)\n" % (
                    len(self.alarm),
                    100 * len(self.alarm) / self.n,
                )
            else:
                s += "\t number of peaks  : %s\n" % self.Nt
                s += "\t extreme quantile : %s\n" % self.extreme_quantile
                s += "Algorithm run : No\n"
        return s

    def fit(self, init_data, data):
        """
        Import data to DSPOT object

        Parameters
            ----------
            init_data : list, numpy.array or pandas.Series
                    initial batch to calibrate the algorithm

        data : numpy.array
                    data for the run (list, np.array or pd.series)

        """
        if isinstance(data, list):
            self.data = np.array(data)
        elif isinstance(data, np.ndarray):
            self.data = data
        elif isinstance(data, pd.Series):
            self.data = data.values
        else:
            print("This data format (%s) is not supported" % type(data))
            return

        if isinstance(init_data, list):
            self.init_data = np.array(init_data)
        elif isinstance(init_data, np.ndarray):
            self.init_data = init_data
        elif isinstance(init_data, pd.Series):
            self.init_data = init_data.values
        elif isinstance(init_data, int):
            self.init_data = self.data[:init_data]
            self.data = self.data[init_data:]
        elif isinstance(init_data, float) & (init_data < 1) & (init_data > 0):
            r = int(init_data * data.size)
            self.init_data = self.data[:r]
            self.data = self.data[r:]
        else:
            print("The initial data cannot be set")
            return

    def add(self, data):
        """
        This function allows to append data to the already fitted data

        Parameters
            ----------
            data : list, numpy.array, pandas.Series
                    data to append
        """
        if isinstance(data, list):
            data = np.array(data)
        elif isinstance(data, np.ndarray):
            data = data
        elif isinstance(data, pd.Series):
            data = data.values
        else:
            print("This data format (%s) is not supported" % type(data))
            return

        self.data = np.append(self.data, data)
        return

    def initialize(self, verbose=True):
        """
        Run the calibration (initialization) step

        Parameters
            ----------
            verbose : bool
                    (default = True) If True, gives details about the batch initialization
        """
        n_init = self.init_data.size - self.depth

        M = backMean(self.init_data, self.depth)
        T = self.init_data[self.depth :] - M[:-1]  # new variable

        S = np.sort(T)  # we sort X to get the empirical quantile
        self.init_threshold = S[int(0.98 * n_init)]  # t is fixed for the whole algorithm

        # initial peaks
        self.peaks = T[T > self.init_threshold] - self.init_threshold
        self.Nt = self.peaks.size
        self.n = n_init

        if verbose:
            print("Initial threshold : %s" % self.init_threshold)
            print("Number of peaks : %s" % self.Nt)
            print("Grimshaw maximum log-likelihood estimation ... ", end="")

        g, s, l = self._grimshaw()
        self.extreme_quantile = self._quantile(g, s)

        if verbose:
            print("[done]")
            print("\t" + chr(0x03B3) + " = " + str(g))
            print("\t" + chr(0x03C3) + " = " + str(s))
            print("\tL = " + str(l))
            print("Extreme quantile (probability = %s): %s" % (self.proba, self.extreme_quantile))

        return

    def _rootsFinder(fun, jac, bounds, npoints, method):
        """
        Find possible roots of a scalar function

        Parameters
        ----------
        fun : function
                    scalar function
        jac : function
            first order derivative of the function
        bounds : tuple
            (min,max) interval for the roots search
        npoints : int
            maximum number of roots to output
        method : str
            'regular' : regular sample of the search interval, 'random' : uniform (distribution) sample of the search interval

        Returns
        ----------
        numpy.array
            possible roots of the function
        """
        if method == "regular":
            step = (bounds[1] - bounds[0]) / (npoints + 1)
            X0 = np.arange(bounds[0] + step, bounds[1], step)
        elif method == "random":
            X0 = np.random.uniform(bounds[0], bounds[1], npoints)

        def objFun(X, f, jac):
            g = 0
            j = np.zeros(X.shape)
            i = 0
            for x in X:
                fx = f(x)
                g = g + fx**2
                j[i] = 2 * fx * jac(x)
                i = i + 1
            return g, j

        opt = minimize(
            lambda X: objFun(X, fun, jac),
            X0,
            method="L-BFGS-B",
            jac=True,
            bounds=[bounds] * len(X0),
        )

        X = opt.x
        np.round(X, decimals=5)
        return np.unique(X)

    def _log_likelihood(Y, gamma, sigma):
        """
        Compute the log-likelihood for the Generalized Pareto Distribution (μ=0)

        Parameters
        ----------
        Y : numpy.array
                    observations
        gamma : float
            GPD index parameter
        sigma : float
            GPD scale parameter (>0)

        Returns
        ----------
        float
            log-likelihood of the sample Y to be drawn from a GPD(γ,σ,μ=0)
        """
        n = Y.size
        if gamma != 0:
            tau = gamma / sigma
            L = -n * log(sigma) - (1 + (1 / gamma)) * (np.log(1 + tau * Y)).sum()
        else:
            L = n * (1 + log(Y.mean()))
        return L

    def _grimshaw(self, epsilon=1e-8, n_points=10):
        """
        Compute the GPD parameters estimation with the Grimshaw's trick

        Parameters
        ----------
        epsilon : float
                    numerical parameter to perform (default : 1e-8)
        n_points : int
            maximum number of candidates for maximum likelihood (default : 10)

        Returns
        ----------
        gamma_best,sigma_best,ll_best
            gamma estimates, sigma estimates and corresponding log-likelihood
        """

        def u(s):
            return 1 + np.log(s).mean()

        def v(s):
            return np.mean(1 / s)

        def w(Y, t):
            s = 1 + t * Y
            us = u(s)
            vs = v(s)
            return us * vs - 1

        def jac_w(Y, t):
            s = 1 + t * Y
            us = u(s)
            vs = v(s)
            jac_us = (1 / t) * (1 - vs)
            jac_vs = (1 / t) * (-vs + np.mean(1 / s**2))
            return us * jac_vs + vs * jac_us

        Ym = self.peaks.min()
        YM = self.peaks.max()
        Ymean = self.peaks.mean()

        a = -1 / YM
        if abs(a) < 2 * epsilon:
            epsilon = abs(a) / n_points

        a = a + epsilon
        b = 2 * (Ymean - Ym) / (Ymean * Ym)
        c = 2 * (Ymean - Ym) / (Ym**2)

        # We look for possible roots
        left_zeros = SPOT._rootsFinder(
            lambda t: w(self.peaks, t),
            lambda t: jac_w(self.peaks, t),
            (a + epsilon, -epsilon),
            n_points,
            "random",
        )

        # right_zeros = SPOT._rootsFinder( lambda t: w(self.peaks, t), lambda t: jac_w(self.peaks, t), (b, c), n_points, "regular")
        right_zeros = SPOT._rootsFinder(
            lambda t: w(self.peaks, t), lambda t: jac_w(self.peaks, t), (b, c), n_points, "random"
        )

        # all the possible roots
        zeros = np.concatenate((left_zeros, right_zeros))

        # 0 is always a solution so we initialize with it
        gamma_best = 0
        sigma_best = Ymean
        ll_best = SPOT._log_likelihood(self.peaks, gamma_best, sigma_best)

        # we look for better candidates
        for z in zeros:
            gamma = u(1 + z * self.peaks) - 1
            sigma = gamma / z
            ll = dSPOT._log_likelihood(self.peaks, gamma, sigma)
            if ll > ll_best:
                gamma_best = gamma
                sigma_best = sigma
                ll_best = ll

        return gamma_best, sigma_best, ll_best

    def _quantile(self, gamma, sigma):
        """
        Compute the quantile at level 1-q

        Parameters
        ----------
        gamma : float
                    GPD parameter
        sigma : float
            GPD parameter

        Returns
        ----------
        float
            quantile at level 1-q for the GPD(γ,σ,μ=0)
        """
        r = self.n * self.proba / self.Nt
        if gamma != 0:
            return self.init_threshold + (sigma / gamma) * (pow(r, -gamma) - 1)
        else:
            return self.init_threshold - sigma * log(r)

    def run(self, with_alarm=True):
        """
        Run biSPOT on the stream

        Parameters
        ----------
        with_alarm : bool
		    (default = True) If False, SPOT will adapt the threshold assuming \
            there is no abnormal values


        Returns
        ----------
        dict
            keys : 'upper_thresholds', 'lower_thresholds' and 'alarms'

            '***-thresholds' contains the extreme quantiles and 'alarms' contains \
            the indexes of the values which have triggered alarms

        """
        if self.n > self.init_data.size:
            print(
                "Warning : the algorithm seems to have already been run, you \
            should initialize before running again"
            )
            return {}

        # actual normal window
        W = self.init_data[-self.depth :]

        # list of the thresholds
        th = []
        alarm = []
        # Loop over the stream
        for i in tqdm.tqdm(range(self.data.size), ascii=True):
            Mi = W.mean()
            # If the observed value exceeds the current threshold (alarm case)
            if (self.data[i] - Mi) > self.extreme_quantile:
                # if we want to alarm, we put it in the alarm list
                if with_alarm:
                    alarm.append(i)
                # otherwise we add it in the peaks
                else:
                    self.peaks = np.append(self.peaks, self.data[i] - Mi - self.init_threshold)
                    self.Nt += 1
                    self.n += 1
                    # and we update the thresholds

                    g, s, l = self._grimshaw()
                    self.extreme_quantile = self._quantile(g, s)  # + Mi
                    W = np.append(W[1:], self.data[i])

            # case where the value exceeds the initial threshold but not the alarm ones
            elif (self.data[i] - Mi) > self.init_threshold:
                # we add it in the peaks
                self.peaks = np.append(self.peaks, self.data[i] - Mi - self.init_threshold)
                self.Nt += 1
                self.n += 1
                # and we update the thresholds

                g, s, l = self._grimshaw()
                self.extreme_quantile = self._quantile(g, s)  # + Mi
                W = np.append(W[1:], self.data[i])
            else:
                self.n += 1
                W = np.append(W[1:], self.data[i])

            th.append(self.extreme_quantile + Mi)  # thresholds record

        return {"thresholds": th, "alarms": alarm}

    def plot(self, run_results, with_alarm=True):
        """
        Plot the results given by the run

        Parameters
        ----------
        run_results : dict
            results given by the 'run' method
        with_alarm : bool
                    (default = True) If True, alarms are plotted.


        Returns
        ----------
        list
            list of the plots

        """
        x = range(self.data.size)
        K = run_results.keys()

        (ts_fig,) = plt.plot(x, self.data, color=air_force_blue)
        fig = [ts_fig]

        #        if 'upper_thresholds' in K:
        #            thup = run_results['upper_thresholds']
        #            uth_fig, = plt.plot(x,thup,color=deep_saffron,lw=2,ls='dashed')
        #            fig.append(uth_fig)
        #
        #        if 'lower_thresholds' in K:
        #            thdown = run_results['lower_thresholds']
        #            lth_fig, = plt.plot(x,thdown,color=deep_saffron,lw=2,ls='dashed')
        #            fig.append(lth_fig)

        if "thresholds" in K:
            th = run_results["thresholds"]
            (th_fig,) = plt.plot(x, th, color=deep_saffron, lw=2, ls="dashed")
            fig.append(th_fig)

        if with_alarm and ("alarms" in K):
            alarm = run_results["alarms"]
            if len(alarm) > 0:
                plt.scatter(alarm, self.data[alarm], color="red")

        plt.xlim((0, self.data.size))

        return fig


"""
=========================== DRIFT & DOUBLE BOUNDS =============================
"""


class bidSPOT:
    """
    This class allows to run DSPOT algorithm on univariate dataset (upper and lower bounds)

    Attributes
    ----------
    proba : float
        Detection level (risk), chosen by the user

    depth : int
        Number of observations to compute the moving average

    extreme_quantile : float
        current threshold (bound between normal and abnormal events)

    data : numpy.array
        stream

    init_data : numpy.array
        initial batch of observations (for the calibration/initialization step)

    init_threshold : float
        initial threshold computed during the calibration step

    peaks : numpy.array
        array of peaks (excesses above the initial threshold)

    n : int
        number of observed values

    Nt : int
        number of observed peaks
    """

    def __init__(self, q=1e-4, depth=10):
        self.proba = q
        self.data = None
        self.init_data = None
        self.n = 0
        self.depth = depth

        nonedict = {"up": None, "down": None}

        self.extreme_quantile = dict.copy(nonedict)
        self.init_threshold = dict.copy(nonedict)
        self.peaks = dict.copy(nonedict)
        self.gamma = dict.copy(nonedict)
        self.sigma = dict.copy(nonedict)
        self.Nt = {"up": 0, "down": 0}

    def __str__(self):
        s = ""
        s += "Streaming Peaks-Over-Threshold Object\n"
        s += "Detection level q = %s\n" % self.proba
        if self.data is not None:
            s += "Data imported : Yes\n"
            s += "\t initialization  : %s values\n" % self.init_data.size
            s += "\t stream : %s values\n" % self.data.size
        else:
            s += "Data imported : No\n"
            return s

        if self.n == 0:
            s += "Algorithm initialized : No\n"
        else:
            s += "Algorithm initialized : Yes\n"
            s += "\t initial threshold : %s\n" % self.init_threshold

            r = self.n - self.init_data.size
            if r > 0:
                s += "Algorithm run : Yes\n"
                s += "\t number of observations : %s (%.2f %%)\n" % (r, 100 * r / self.n)
                s += "\t triggered alarms : %s (%.2f %%)\n" % (
                    len(self.alarm),
                    100 * len(self.alarm) / self.n,
                )
            else:
                s += "\t number of peaks  : %s\n" % self.Nt
                s += "\t upper extreme quantile : %s\n" % self.extreme_quantile["up"]
                s += "\t lower extreme quantile : %s\n" % self.extreme_quantile["down"]
                s += "Algorithm run : No\n"
        return s

    def fit(self, init_data, data):
        """
        Import data to biDSPOT object

        Parameters
            ----------
            init_data : list, numpy.array or pandas.Series
                    initial batch to calibrate the algorithm

        data : numpy.array
                    data for the run (list, np.array or pd.series)

        """
        if isinstance(data, list):
            self.data = np.array(data)
        elif isinstance(data, np.ndarray):
            self.data = data
        elif isinstance(data, pd.Series):
            self.data = data.values
        else:
            print("This data format (%s) is not supported" % type(data))
            return

        if isinstance(init_data, list):
            self.init_data = np.array(init_data)
        elif isinstance(init_data, np.ndarray):
            self.init_data = init_data
        elif isinstance(init_data, pd.Series):
            self.init_data = init_data.values
        elif isinstance(init_data, int):
            self.init_data = self.data[:init_data]
            self.data = self.data[init_data:]
        elif isinstance(init_data, float) & (init_data < 1) & (init_data > 0):
            r = int(init_data * data.size)
            self.init_data = self.data[:r]
            self.data = self.data[r:]
        else:
            print("The initial data cannot be set")
            return

    def add(self, data):
        """
        This function allows to append data to the already fitted data

        Parameters
            ----------
            data : list, numpy.array, pandas.Series
                    data to append
        """
        if isinstance(data, list):
            data = np.array(data)
        elif isinstance(data, np.ndarray):
            data = data
        elif isinstance(data, pd.Series):
            data = data.values
        else:
            print("This data format (%s) is not supported" % type(data))
            return

        self.data = np.append(self.data, data)
        return

    def initialize(self, verbose=True):
        """
        Run the calibration (initialization) step

        Parameters
            ----------
            verbose : bool
                    (default = True) If True, gives details about the batch initialization
        """
        n_init = self.init_data.size - self.depth

        M = backMean(self.init_data, self.depth)
        T = self.init_data[self.depth :] - M[:-1]  # new variable

        S = np.sort(T)  # we sort T to get the empirical quantile
        self.init_threshold["up"] = S[int(0.98 * n_init)]  # t is fixed for the whole algorithm
        self.init_threshold["down"] = S[int(0.02 * n_init)]  # t is fixed for the whole algorithm

        # initial peaks
        self.peaks["up"] = T[T > self.init_threshold["up"]] - self.init_threshold["up"]
        self.peaks["down"] = -(T[T < self.init_threshold["down"]] - self.init_threshold["down"])
        self.Nt["up"] = self.peaks["up"].size
        self.Nt["down"] = self.peaks["down"].size
        self.n = n_init

        if verbose:
            print("Initial threshold : %s" % self.init_threshold)
            print("Number of peaks : %s" % self.Nt)
            print("Grimshaw maximum log-likelihood estimation ... ", end="")

        l = {"up": None, "down": None}
        for side in ["up", "down"]:
            g, s, l[side] = self._grimshaw(side)
            self.extreme_quantile[side] = self._quantile(side, g, s)
            self.gamma[side] = g
            self.sigma[side] = s

        ltab = 20
        form = "\t" + "%20s" + "%20.2f" + "%20.2f"
        if verbose:
            print("[done]")
            print("\t" + "Parameters".rjust(ltab) + "Upper".rjust(ltab) + "Lower".rjust(ltab))
            print("\t" + "-" * ltab * 3)
            print(form % (chr(0x03B3), self.gamma["up"], self.gamma["down"]))
            print(form % (chr(0x03C3), self.sigma["up"], self.sigma["down"]))
            print(form % ("likelihood", l["up"], l["down"]))
            print(
                form
                % ("Extreme quantile", self.extreme_quantile["up"], self.extreme_quantile["down"])
            )
            print("\t" + "-" * ltab * 3)
        return

    def _rootsFinder(fun, jac, bounds, npoints, method):
        """
        Find possible roots of a scalar function

        Parameters
        ----------
        fun : function
                    scalar function
        jac : function
            first order derivative of the function
        bounds : tuple
            (min,max) interval for the roots search
        npoints : int
            maximum number of roots to output
        method : str
            'regular' : regular sample of the search interval, 'random' : uniform (distribution) sample of the search interval

        Returns
        ----------
        numpy.array
            possible roots of the function
        """
        if method == "regular":
            step = (bounds[1] - bounds[0]) / (npoints + 1)
            X0 = np.arange(bounds[0] + step, bounds[1], step)
        elif method == "random":
            X0 = np.random.uniform(bounds[0], bounds[1], npoints)

        def objFun(X, f, jac):
            g = 0
            j = np.zeros(X.shape)
            i = 0
            for x in X:
                fx = f(x)
                g = g + fx**2
                j[i] = 2 * fx * jac(x)
                i = i + 1
            return g, j

        opt = minimize(
            lambda X: objFun(X, fun, jac),
            X0,
            method="L-BFGS-B",
            jac=True,
            bounds=[bounds] * len(X0),
        )

        X = opt.x
        np.round(X, decimals=5)
        return np.unique(X)

    def _log_likelihood(Y, gamma, sigma):
        """
        Compute the log-likelihood for the Generalized Pareto Distribution (μ=0)

        Parameters
        ----------
        Y : numpy.array
                    observations
        gamma : float
            GPD index parameter
        sigma : float
            GPD scale parameter (>0)

        Returns
        ----------
        float
            log-likelihood of the sample Y to be drawn from a GPD(γ,σ,μ=0)
        """
        n = Y.size
        if gamma != 0:
            tau = gamma / sigma
            L = -n * log(sigma) - (1 + (1 / gamma)) * (np.log(1 + tau * Y)).sum()
        else:
            L = n * (1 + log(Y.mean()))
        return L

    def _grimshaw(self, side, epsilon=1e-8, n_points=8):
        """
        Compute the GPD parameters estimation with the Grimshaw's trick

        Parameters
        ----------
        epsilon : float
                    numerical parameter to perform (default : 1e-8)
        n_points : int
            maximum number of candidates for maximum likelihood (default : 10)

        Returns
        ----------
        gamma_best,sigma_best,ll_best
            gamma estimates, sigma estimates and corresponding log-likelihood
        """

        def u(s):
            return 1 + np.log(s).mean()

        def v(s):
            return np.mean(1 / s)

        def w(Y, t):
            s = 1 + t * Y
            us = u(s)
            vs = v(s)
            return us * vs - 1

        def jac_w(Y, t):
            s = 1 + t * Y
            us = u(s)
            vs = v(s)
            jac_us = (1 / t) * (1 - vs)
            jac_vs = (1 / t) * (-vs + np.mean(1 / s**2))
            return us * jac_vs + vs * jac_us

        Ym = self.peaks[side].min()
        YM = self.peaks[side].max()
        Ymean = self.peaks[side].mean()

        a = -1 / YM
        if abs(a) < 2 * epsilon:
            epsilon = abs(a) / n_points

        a = a + epsilon
        b = 2 * (Ymean - Ym) / (Ymean * Ym)
        c = 2 * (Ymean - Ym) / (Ym**2)

        # We look for possible roots
        left_zeros = bidSPOT._rootsFinder(
            lambda t: w(self.peaks[side], t),
            lambda t: jac_w(self.peaks[side], t),
            (a + epsilon, -epsilon),
            n_points,
            "regular",
        )

        right_zeros = bidSPOT._rootsFinder(
            lambda t: w(self.peaks[side], t),
            lambda t: jac_w(self.peaks[side], t),
            (b, c),
            n_points,
            "regular",
        )

        # all the possible roots
        zeros = np.concatenate((left_zeros, right_zeros))

        # 0 is always a solution so we initialize with it
        gamma_best = 0
        sigma_best = Ymean
        ll_best = bidSPOT._log_likelihood(self.peaks[side], gamma_best, sigma_best)

        # we look for better candidates
        for z in zeros:
            gamma = u(1 + z * self.peaks[side]) - 1
            sigma = gamma / z
            ll = bidSPOT._log_likelihood(self.peaks[side], gamma, sigma)
            if ll > ll_best:
                gamma_best = gamma
                sigma_best = sigma
                ll_best = ll

        return gamma_best, sigma_best, ll_best

    def _quantile(self, side, gamma, sigma):
        """
        Compute the quantile at level 1-q for a given side

        Parameters
        ----------
        side : str
            'up' or 'down'
        gamma : float
                    GPD parameter
        sigma : float
            GPD parameter

        Returns
        ----------
        float
            quantile at level 1-q for the GPD(γ,σ,μ=0)
        """
        if side == "up":
            r = self.n * self.proba / self.Nt[side]
            if gamma != 0:
                return self.init_threshold["up"] + (sigma / gamma) * (pow(r, -gamma) - 1)
            else:
                return self.init_threshold["up"] - sigma * log(r)
        elif side == "down":
            r = self.n * self.proba / self.Nt[side]
            if gamma != 0:
                return self.init_threshold["down"] - (sigma / gamma) * (pow(r, -gamma) - 1)
            else:
                return self.init_threshold["down"] + sigma * log(r)
        else:
            print("error : the side is not right")

    def run(self, with_alarm=True, plot=True):
        """
        Run biDSPOT on the stream

        Parameters
        ----------
        with_alarm : bool
		    (default = True) If False, SPOT will adapt the threshold assuming \
            there is no abnormal values


        Returns
        ----------
        dict
            keys : 'upper_thresholds', 'lower_thresholds' and 'alarms'

            '***-thresholds' contains the extreme quantiles and 'alarms' contains \
            the indexes of the values which have triggered alarms

        """
        if self.n > self.init_data.size:
            print(
                "Warning : the algorithm seems to have already been run, you \
            should initialize before running again"
            )
            return {}

        # actual normal window
        W = self.init_data[-self.depth :]

        # list of the thresholds
        thup = []
        thdown = []
        alarm = []
        # Loop over the stream
        for i in tqdm.tqdm(range(self.data.size), ascii=True):
            Mi = W.mean()
            Ni = self.data[i] - Mi
            # If the observed value exceeds the current threshold (alarm case)
            if Ni > self.extreme_quantile["up"]:
                # if we want to alarm, we put it in the alarm list
                if with_alarm:
                    alarm.append(i)
                # otherwise we add it in the peaks
                else:
                    self.peaks["up"] = np.append(self.peaks["up"], Ni - self.init_threshold["up"])
                    self.Nt["up"] += 1
                    self.n += 1
                    # and we update the thresholds

                    g, s, l = self._grimshaw("up")
                    self.extreme_quantile["up"] = self._quantile("up", g, s)
                    W = np.append(W[1:], self.data[i])

            # case where the value exceeds the initial threshold but not the alarm ones
            elif Ni > self.init_threshold["up"]:
                # we add it in the peaks
                self.peaks["up"] = np.append(self.peaks["up"], Ni - self.init_threshold["up"])
                self.Nt["up"] += 1
                self.n += 1
                # and we update the thresholds
                g, s, l = self._grimshaw("up")
                self.extreme_quantile["up"] = self._quantile("up", g, s)
                W = np.append(W[1:], self.data[i])

            elif Ni < self.extreme_quantile["down"]:
                # if we want to alarm, we put it in the alarm list
                if with_alarm:
                    alarm.append(i)
                # otherwise we add it in the peaks
                else:
                    self.peaks["down"] = np.append(
                        self.peaks["down"], -(Ni - self.init_threshold["down"])
                    )
                    self.Nt["down"] += 1
                    self.n += 1
                    # and we update the thresholds

                    g, s, l = self._grimshaw("down")
                    self.extreme_quantile["down"] = self._quantile("down", g, s)
                    W = np.append(W[1:], self.data[i])

            # case where the value exceeds the initial threshold but not the alarm ones
            elif Ni < self.init_threshold["down"]:
                # we add it in the peaks
                self.peaks["down"] = np.append(
                    self.peaks["down"], -(Ni - self.init_threshold["down"])
                )
                self.Nt["down"] += 1
                self.n += 1
                # and we update the thresholds

                g, s, l = self._grimshaw("down")
                self.extreme_quantile["down"] = self._quantile("down", g, s)
                W = np.append(W[1:], self.data[i])
            else:
                self.n += 1
                W = np.append(W[1:], self.data[i])

            thup.append(self.extreme_quantile["up"] + Mi)  # upper thresholds record
            thdown.append(self.extreme_quantile["down"] + Mi)  # lower thresholds record

        return {"upper_thresholds": thup, "lower_thresholds": thdown, "alarms": alarm}

    def plot(self, run_results, with_alarm=True):
        """
        Plot the results given by the run

        Parameters
        ----------
        run_results : dict
            results given by the 'run' method
        with_alarm : bool
                    (default = True) If True, alarms are plotted.


        Returns
        ----------
        list
            list of the plots

        """
        x = range(self.data.size)
        K = run_results.keys()

        (ts_fig,) = plt.plot(x, self.data, color=air_force_blue)
        fig = [ts_fig]

        if "upper_thresholds" in K:
            thup = run_results["upper_thresholds"]
            (uth_fig,) = plt.plot(x, thup, color=deep_saffron, lw=2, ls="dashed")
            fig.append(uth_fig)

        if "lower_thresholds" in K:
            thdown = run_results["lower_thresholds"]
            (lth_fig,) = plt.plot(x, thdown, color=deep_saffron, lw=2, ls="dashed")
            fig.append(lth_fig)

        if with_alarm and ("alarms" in K):
            alarm = run_results["alarms"]
            if len(alarm) > 0:
                al_fig = plt.scatter(alarm, self.data[alarm], color="red")
                fig.append(al_fig)

        plt.xlim((0, self.data.size))

        return fig


def run_SPOT(data, q=1e-3, d=30, n_init=None):
    node_names = list(range(1, data.shape[1] + 1))

    result_dict = {}
    if n_init is None:
        n_init = int(0.2 * len(data))
    for svc_id in range(len(node_names)):
        print("{:-^40}".format("svc_id: {}".format(svc_id)))
        init_data = data[:n_init, svc_id]  # initial batch
        _data = data[n_init:, svc_id]  # stream
        # q: risk parameter
        # d: depth parameter
        s = dSPOT(q, d)  # DSPOT object
        s.fit(init_data, _data)  # data import
        s.initialize()  # initialization step
        results = s.run()  # run
        #     s.plot(results) 	 	# plot
        result_dict[svc_id] = results
    return result_dict


def get_Q_matrix(g, rho=0.2):
    corr = np.corrcoef(np.array(data).T)
    for i in range(corr.shape[0]):
        corr[i, i] = 0.0
    corr = np.abs(corr)

    Q = np.zeros([len(node_names), len(node_names)])
    for e in g.edges():
        Q[e[0], e[1]] = corr[frontend[0] - 1, e[1]]
        backward_e = (e[1], e[0])
        if backward_e not in g.edges():
            Q[e[1], e[0]] = rho * corr[frontend[0] - 1, e[0]]

    adj = nx.adj_matrix(g).todense()
    for i in range(len(node_names)):
        P_pc_max = None
        res_l = np.array([corr[frontend[0] - 1, k] for k in adj[:, i]])
        if corr[frontend[0] - 1, i] > np.max(res_l):
            Q[i, i] = corr[frontend[0] - 1, i] - np.max(res_l)
        else:
            Q[i, i] = 0
    l = []
    for i in np.sum(Q, axis=1):
        if i > 0:
            l.append(1.0 / i)
        else:
            l.append(0.0)
    l = np.diag(l)
    Q = np.dot(l, Q)
    return Q


def randomwalk(
    P,
    epochs,
    start_node,
    teleportation_prob,
    walk_step=50,
    print_trace=False,
):
    n = P.shape[0]
    score = np.zeros([n])
    current = start_node - 1
    for epoch in range(epochs):
        current = start_node - 1
        if print_trace:
            print("\n{:2d}".format(current + 1), end="->")
        for step in range(walk_step):
            if np.sum(P[current]) == 0:
                break
            else:
                next_node = np.random.choice(range(n), p=P[current])
                if print_trace:
                    print("{:2d}".format(current + 1), end="->")
                score[next_node] += 1
                current = next_node
    label = [i for i in range(n)]
    score_list = list(zip(label, score))
    score_list.sort(key=lambda x: x[1], reverse=True)
    return score_list


def microcause(
    data: pd.DataFrame, inject_time=None, dataset=None, num_loop=None, sli=None, **kwargs
):
    data = preprocess(
        data=data, dataset=dataset, dk_select_useful=kwargs.get("dk_select_useful", False)
    )
    np_data = data.to_numpy().astype(float)
    node_names = data.columns.to_list()

    frontend = [node_names.index(sli) + 1]

    # try:
    # SPOT_res = run_SPOT(np_data, q=1e-3, d=50)

    def get_Q_matrix_part_corr(g, rho=0.2):
        # df = pd.DataFrame(np_data, columns=node_names)
        df = data

        def get_part_corr(x, y):
            cond = get_confounders(y)
            if x in cond:
                cond.remove(x)
            if y in cond:
                cond.remove(y)
            ret = partial_corr(
                data=df,
                x=df.columns[x],
                y=df.columns[y],
                covar=[df.columns[_] for _ in cond],
                method="pearson",
            )
            # For a valid transition probability, use absolute correlation values.
            return abs(float(ret.r))

        # Calculate the parent nodes set.
        pa_set = {}
        for e in g.edges():
            # Skip self links.
            if e[0] == e[1]:
                continue
            if e[1] not in pa_set:
                pa_set[e[1]] = set([e[0]])
            else:
                pa_set[e[1]].add(e[0])
        # Set an empty set for the nodes without parent nodes.
        for n in g.nodes():
            if n not in pa_set:
                pa_set[n] = set([])

        def get_confounders(j: int):
            ret = pa_set[frontend[0] - 1].difference([j])
            ret = ret.union(pa_set[j])
            return ret

        Q = np.zeros([len(node_names), len(node_names)])
        for e in g.edges():
            # Do not add self links.
            if e[0] == e[1]:
                continue
            # e[0] --> e[1]: cause --> result
            # Forward step.
            # Note for partial correlation, the two variables cannot be the same.
            if frontend[0] - 1 != e[0]:
                Q[e[1], e[0]] = get_part_corr(frontend[0] - 1, e[0])
            # Backward step
            backward_e = (e[1], e[0])
            # Note for partial correlation, the two variables cannot be the same.
            if backward_e not in g.edges() and frontend[0] - 1 != e[1]:
                Q[e[0], e[1]] = rho * get_part_corr(frontend[0] - 1, e[1])

        adj = nx.adj_matrix(g).todense()
        for i in range(len(node_names)):
            # Calculate P_pc^max
            P_pc_max = []
            # (k, i) in edges.
            for k in adj[:, i].nonzero()[0]:
                # Note for partial correlation, the two variables cannot be the same.
                if frontend[0] - 1 != k:
                    P_pc_max.append(get_part_corr(frontend[0] - 1, k))
            if len(P_pc_max) > 0:
                P_pc_max = np.max(P_pc_max)
            else:
                P_pc_max = 0

            # Note for partial correlation, the two variables cannot be the same.
            if frontend[0] - 1 != i:
                q_ii = get_part_corr(frontend[0] - 1, i)
                if q_ii > P_pc_max:
                    Q[i, i] = q_ii - P_pc_max
                else:
                    Q[i, i] = 0

        l = []
        for i in np.sum(Q, axis=1):
            if i > 0:
                l.append(1.0 / i)
            else:
                l.append(0.0)
        l = np.diag(l)
        Q = np.dot(l, Q)
        return Q

    def get_eta(SPOT_res, n_init):
        eta = np.zeros([len(node_names)])
        ab_timepoint = [0 for i in range(len(node_names))]
        for svc_id in range(len(node_names)):
            mask = np_data[n_init:, svc_id] > np.array(SPOT_res[svc_id]["thresholds"])
            ratio = np.abs(
                np_data[n_init:, svc_id] - np.array(SPOT_res[svc_id]["thresholds"])
            ) / np.array(SPOT_res[svc_id]["thresholds"])
            if mask.nonzero()[0].shape[0] > 0:
                eta[svc_id] = np.max(ratio[mask.nonzero()[0]])
                ab_timepoint[svc_id] = np.min(mask.nonzero()[0])
            else:
                eta[svc_id] = 0
        return eta, ab_timepoint

    # eta, ab_timepoint = get_eta(SPOT_res, int(0.5 * len(np_data)))
    eta = np.ones([len(node_names)])

    def run_pcmci(data, pc_alpha=0.1, verbosity=0):
        dataframe = pp.DataFrame(data)
        cond_ind_test = ParCorr()
        pcmci = PCMCI(dataframe=dataframe, cond_ind_test=cond_ind_test, verbosity=verbosity)
        pcmci_res = pcmci.run_pcmci(tau_max=10, pc_alpha=pc_alpha)
        return pcmci, pcmci_res

    pcmci, pcmci_res = run_pcmci(np_data, pc_alpha=0.1, verbosity=0)

    def get_links(pcmci, results, alpha_level=0.01):
        pcmci_links = pcmci.return_significant_links(
            results["p_matrix"],
            results["val_matrix"],
            alpha_level=alpha_level,
            include_lagzero_links=False,
        )
        g = nx.DiGraph()
        for i in range(len(node_names)):
            g.add_node(i)
        for n, links in pcmci_links["link_dict"].items():
            for l in links:
                g.add_edge(n, l[0])
        return g

    g = get_links(pcmci, pcmci_res, alpha_level=0.001)
    Q = get_Q_matrix_part_corr(g, rho=0.2)

    vis_list = randomwalk(Q, 1000, frontend[0], teleportation_prob=0, walk_step=1000)

    def get_gamma(score_list, eta, lambda_param=0.8):
        gamma = [0 for _ in range(len(node_names))]
        max_vis_time = np.max([i[1] for i in score_list])
        #     max_vis_time = 1.0
        max_eta = np.max(eta)
        for n, vis in score_list:
            gamma[n] = lambda_param * vis / max_vis_time + (1 - lambda_param) * eta[n] / max_eta
        return gamma

    gamma = get_gamma(vis_list, eta, lambda_param=0.5)

    score_list = sorted(
        zip([(i + 1) for i in range(len(node_names))], gamma), key=lambda x: x[1], reverse=True
    )

    """
    [(4, 0.5456346812449508),
     (3, 0.522603027996977),
      (8, 0.5),
       (9, 0.042577533064681955),
        (2, 0.03943466310564701),
         (6, 0.03290640165083645),
          (1, 0.0247683398383279),
           (7, 0.007963883394528627),
            (10, 0.0027164158244277915),
             (5, 0.0)]
    """
    ranks = []
    for r in score_list:  # (10, 1032.)
        r = r[0]  # 10
        # node_names[idx - 1]
        ranks.append(node_names[r - 1])

    return {
        "adj": nx.adjacency_matrix(g).todense(),
        "node_names": node_names,
        "ranks": ranks,
    }

    # except Exception as e:
    #     print("==== EXCEPTION ====")
    #     print(e)
    #     print("==== EXCEPTION ====")
    #     return {
    #         "ranks": node_names
    #     }