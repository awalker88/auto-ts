import warnings
from dateutil.relativedelta import relativedelta
import datetime as dt
from functools import reduce
from typing import Union, List, Tuple

import pandas as pd
import numpy as np
from pmdarima import auto_arima
from statsmodels.tsa.holtwinters import ExponentialSmoothing
from AutoTS.utils.error_metrics import mase, mse, rmse
# import matplotlib.pyplot as plot
from tbats import BATS

from AutoTS.utils import validation as val
from AutoTS.utils.CandidateModel import CandidateModel


class AutoTS:
    """
    Automatic modeler that finds the best time-series method to model your data
    :param model_names: Models to consider when fitting. Currently supported models are
    'auto_arima', 'exponential_smoothing', 'tbats', and 'ensemble'. default is all available models
    :param error_metric: Which error metric to use when ranking models. Currently supported metrics
    are 'mase', 'mse', and 'rmse'. default='mase'
    :param seasonal_period: period of the data's seasonal trend. 3 would mean your data has quarterly
    trends. Supported models can use multiple seasonalities if a list is provided (Non-supported models
    will use the first item in list). None implies no seasonality.
    :param holdout_period: number of periods to leave out as a test set when comparing candidate models.
    default=4
    """
    def __init__(self,
                 model_names: Union[Tuple[str], List[str]] = ('auto_arima', 'exponential_smoothing',
                                                              'tbats', 'ensemble'),
                 error_metric: str = 'mase',
                 seasonal_period: Union[int, List[int]] = None,
                 # seasonality_mode: str = 'm',
                 holdout_period: int = 4,
                 verbose: bool = False,
                 auto_arima_args: dict = None,
                 exponential_smoothing_args: dict = None,
                 tbats_args: dict = None
                 ):

        # fix mutable args
        if auto_arima_args is None:
            auto_arima_args = {}
        if exponential_smoothing_args is None:
            exponential_smoothing_args = {}
        if tbats_args is None:
            tbats_args = {}

        # input validation
        val.check_models(model_names)
        valid_error_metrics = ['mase', 'mse', 'rmse']
        if error_metric.lower() not in valid_error_metrics:
            raise ValueError(f'Error metric must be one of {valid_error_metrics}')

        self.model_names = [model.lower() for model in model_names]
        self.error_metric = error_metric.lower()
        self.is_seasonal = True if seasonal_period is not None else False
        self.seasonal_period = [seasonal_period] if isinstance(seasonal_period, int) else seasonal_period
        self.holdout_period = holdout_period
        self.verbose = verbose
        self.auto_arima_args = auto_arima_args
        self.exponential_smoothing_args = exponential_smoothing_args
        self.tbats_args = tbats_args

        # Set during fitting or by other methods
        self.data = None
        self.training_data = None
        self.testing_data = None
        self.series_column_name = None
        self.exogenous = None
        self.using_exogenous = False
        self.candidate_models = []
        self.fit_model = None
        self.fit_model_type = None
        self.best_model_error = None
        self.is_fitted = False

        warnings.filterwarnings('ignore', module='statsmodels')

    def fit(self, data: pd.DataFrame, series_column_name: str, exogenous: Union[str, list] = None) -> None:
        """
        Fit model to given training data. Currently assumes your data is monthly
        :param data: pandas dataframe containing series you would like to predict and any exogenous
        variables you'd like to be used. The dataframe's index MUST be a datetime index
        :param series_column_name: name of the column containing the series you would like to predict
        :param exogenous: column name or list of column names you would like to be used as exogenous
        regressors. auto_arima is the only model that supports exogenous regressors. The repressor
        columns should not be a constant or a trend
        """
        val.check_datetime_index(data)
        self._set_input_data(data, series_column_name)

        # if user passes a string value (single column), make sure we can always assume exogenous is a list
        if isinstance(exogenous, str):
            exogenous = [exogenous]

        if exogenous is not None:
            self.using_exogenous = True
            self.exogenous = exogenous

        if 'auto_arima' in self.model_names:
            self.candidate_models.append(self._fit_auto_arima(use_full_dataset=True))
            if self.verbose:
                print(f'\tTrained auto_arima model with error {self.candidate_models[-1].error}')
        if 'exponential_smoothing' in self.model_names:
            self.candidate_models.append(self._fit_exponential_smoothing(use_full_dataset=True))
            if self.verbose:
                print(f'\tTrained exponential_smoothing model with error {self.candidate_models[-1].error}')
        if 'tbats' in self.model_names:
            self.candidate_models.append(self._fit_tbats(use_full_dataset=True))
            if self.verbose:
                print(f'\tTrained tbats model with error {self.candidate_models[-1].error}')
        if 'ensemble' in self.model_names:
            if self.candidate_models is None:
                raise ValueError('No candidate models to ensemble')
            self.candidate_models.append(self._fit_ensemble())
            if self.verbose:
                print(f'\tTrained ensemble model with error {self.candidate_models[-1][0]}')

        # candidate_models[x][0] = model's error
        # candidate_models[x][1] = model object
        # candidate_models[x][2] = model's name
        # candidate_models[x][3] = model's predictions for the test set
        self.candidate_models = sorted(self.candidate_models, key=lambda x: x.error)
        self.best_model_error = self.candidate_models[0].error
        self.fit_model = self.candidate_models[0].fit_model
        self.fit_model_type = self.candidate_models[0].model_type
        self.is_fitted = True

    def _fit_auto_arima(self, use_full_dataset: bool = False) -> CandidateModel:
        """
        Fits an ARIMA model using pmdarima's auto_arima
        :param use_full_dataset: Whether to use the full set of data provided during fit, or use the
        subset training data
        :return: Currently returns a list where the first item is the error on the test set, the
        second is the arima model, the third is the name of the model, and the fourth is the
        predictions made on the test set
        """
        model_data = self.training_data
        if use_full_dataset:
            model_data = self.data

        train_exog = None
        test_exog = None
        if self.using_exogenous:
            train_exog = model_data[self.exogenous]
            test_exog = self.testing_data[self.exogenous]

        auto_arima_seasonal_period = self.seasonal_period
        if self.seasonal_period is None:
            auto_arima_seasonal_period = 1  # need to use auto_arima default if there's no seasonality defined
        else:
            # since auto_arima supports only 1 seasonality, select the first one as "main" seasonality
            auto_arima_seasonal_period = auto_arima_seasonal_period[0]

        try:
            model = auto_arima(model_data[self.series_column_name],
                               # error_action='ignore',
                               supress_warning=True,
                               seasonal=self.is_seasonal, m=auto_arima_seasonal_period,
                               exogenous=train_exog,
                               **self.auto_arima_args
                               )

        # occasionally while determining the necessary level of seasonal differencing, we get a weird
        # numpy dot product error due to array sizes mismatching. If that happens, we try using
        # Canova-Hansen test for seasonal differencing instead
        except ValueError:
            if 'seasonal_test' in self.auto_arima_args.keys() and self.auto_arima_args['seasonal_test'] == 'ocsb':
                warnings.warn('Forcing `seasonal_test="ch"` as "ocsb" occasionally causes numpy errors')
            self.auto_arima_args['seasonal_test'] = 'ch'
            model = auto_arima(model_data[self.series_column_name],
                               # error_action='ignore',
                               supress_warning=True,
                               seasonal=self.is_seasonal, m=auto_arima_seasonal_period,
                               exogenous=train_exog,
                               **self.auto_arima_args
                               )

        test_predictions = pd.DataFrame({'actuals': self.testing_data[self.series_column_name],
                                         'aa_test_predictions': model.predict(
                                             n_periods=len(self.testing_data),
                                             exogenous=test_exog
                                         )})

        test_error = self._error_metric(test_predictions, 'aa_test_predictions', 'actuals')

        # if we didn't use all available data when training, we'll update it with the testing data
        # since we already have the testing error
        if not use_full_dataset:
            model.update(self.testing_data[self.series_column_name])

        return CandidateModel(test_error, model, 'auto_arima', test_predictions)

    def _fit_exponential_smoothing(self, use_full_dataset: bool = False) -> CandidateModel:
        """
        Fits an exponential smoothing model using statsmodels's ExponentialSmoothing model
        :param use_full_dataset: Whether to use the full set of data provided during fit, or use the
        subset training data
        :return: Currently returns a list where the first item is the error on the test set, the
        second is the exponential smoothing model, the third is the name of the model, and the
        fourth is the predictions made on the test set
        """
        model_data = self.data if use_full_dataset else self.training_data

        # if user doesn't specify with kwargs, set these defaults
        if 'trend' not in self.exponential_smoothing_args.keys():
            self.exponential_smoothing_args['trend'] = 'add'
        if 'seasonal' not in self.exponential_smoothing_args.keys():
            self.exponential_smoothing_args['seasonal'] = 'add' if self.seasonal_period is not None else None

        es_seasonal_period = self.seasonal_period
        if self.seasonal_period is not None:
            es_seasonal_period = es_seasonal_period[0]  # es supports only 1 seasonality

        model = ExponentialSmoothing(model_data[self.series_column_name],
                                     seasonal_periods=es_seasonal_period,
                                     **self.exponential_smoothing_args
                                     ).fit()

        test_predictions = pd.DataFrame(
            {'actuals': self.testing_data[self.series_column_name],
             'es_test_predictions': model.predict(
                 self.testing_data.index[-self.holdout_period], self.testing_data.index[-1]
             )})

        error = self._error_metric(test_predictions, 'es_test_predictions', 'actuals')

        return CandidateModel(error, model, 'exponential_smoothing', test_predictions)

    def _fit_tbats(self, use_full_dataset: bool = False, use_simple_model: bool = True) -> CandidateModel:
        """
        Fits a BATS model using tbats's BATS model
        :param use_full_dataset: Whether to use the full set of data provided during fit, or use the
        subset training data
        :return: Currently returns a list where the first item is the error on the test set, the
        second is the BATS model, the third is the name of the model, and the
        fourth is the predictions made on the test set
        """
        if use_full_dataset:
            model_data = self.data
        else:
            model_data = self.training_data

        tbats_seasonal_periods = self.seasonal_period
        if self.seasonal_period is not None:
            tbats_seasonal_periods = self.seasonal_period

        # if user doesn't specify with kwargs, set these defaults
        if 'n_jobs' not in self.tbats_args.keys():
            self.tbats_args['n_jobs'] = 1
        if 'use_arma_errors' not in self.tbats_args.keys():
            self.tbats_args['use_arma_errors'] = False  # helps speed up modeling a bit

        model = BATS(seasonal_periods=tbats_seasonal_periods, use_box_cox=False, **self.tbats_args)
        fit_model = model.fit(model_data[self.series_column_name])

        test_predictions = pd.DataFrame({'actuals': self.testing_data[self.series_column_name],
                                         'tb_test_predictions': fit_model.forecast(len(self.testing_data))})
        error = self._error_metric(test_predictions, 'tb_test_predictions', 'actuals')

        return CandidateModel(error, fit_model, 'tbats', test_predictions)

    def _fit_ensemble(self) -> CandidateModel:
        """
        Fits a model that is the ensemble of all other models specified during AutoTS's initialization
        :return: Currently returns a list where the first item is the error on the test set, the
        second is the exponential smoothing model, the third is the name of the model, and the
        fourth is the predictions made on the test set
        """
        model_predictions = [candidate.predictions for candidate in self.candidate_models]
        all_predictions = reduce(lambda left, right: pd.merge(left, right.drop('actuals', axis='columns'),
                                                              left_index=True, right_index=True),
                                 model_predictions)
        predictions_columns = [col for col in all_predictions.columns if str(col).endswith('predictions')]
        all_predictions['en_test_predictions'] = all_predictions[predictions_columns].mean(axis='columns')

        error = self._error_metric(all_predictions, 'en_test_predictions', 'actuals')

        return CandidateModel(error, None, 'ensemble', all_predictions[['actuals', 'en_test_predictions']])

    def _error_metric(self, data: pd.DataFrame, predictions_column: str, actuals_column: str) -> float:
        """
        Computes error using the error metric specified during initialization
        :param data: pandas dataframe containing predictions and actuals
        :param predictions_column: name of the predictions column
        :param actuals_column: name of the actuals column
        :return: error for given data
        """
        if self.error_metric == 'mase':
            return mase(data, predictions_column, actuals_column)
        if self.error_metric == 'mse':
            return mse(data, predictions_column, actuals_column)
        if self.error_metric == 'rmse':
            return rmse(data, predictions_column, actuals_column)

    def _set_input_data(self, data: pd.DataFrame, series_column_name: str):
        """Sets datasets at class level"""
        self.data = data
        self.training_data = data.iloc[:-self.holdout_period, :]
        self.testing_data = data.iloc[-self.holdout_period:, :]
        self.series_column_name = series_column_name

    def _predict_auto_arima(self, start_date: dt.datetime, end_date: dt.datetime,
                            last_data_date: dt.datetime, exogenous: pd.DataFrame = None) -> pd.Series:
        """Uses a fit ARIMA model to predict between the given dates"""
        # start date and end date are both in-sample
        if start_date < self.data.index[-1] and end_date <= self.data.index[-1]:
            preds = self.fit_model.predict_in_sample(start=self.data.index.get_loc(start_date),
                                                     end=self.data.index.get_loc(end_date),
                                                     exogenous=exogenous)

        # start date is in-sample but end date is not
        elif start_date < self.data.index[-1] < end_date:
            num_extra_months = (end_date.year - last_data_date.year) * 12 + (end_date.month - last_data_date.month)

            # get all in sample predictions and stitch them together with out of sample predictions
            in_sample_preds = self.fit_model.predict_in_sample(start=self.data.index.get_loc(start_date))
            out_of_sample_preds = self.fit_model.predict(num_extra_months)
            preds = np.concatenate([in_sample_preds, out_of_sample_preds])

        # only possible scenario at this point is start date is 1 month past last data date
        else:
            months_to_predict = (end_date.year - start_date.year) * 12 + (end_date.month - start_date.month) + 1
            preds = self.fit_model.predict(months_to_predict, exogenous=exogenous)

        return pd.Series(preds, index=pd.date_range(start_date, end_date, freq='MS'))

    def _predict_exponential_smoothing(self, start_date: dt.datetime, end_date: dt.datetime) -> pd.Series:
        """Uses a fit exponential smoothing model to predict between the given dates"""
        return self.fit_model.predict(start=start_date, end=end_date)

    def _predict_tbats(self, start_date: dt.datetime, end_date: dt.datetime, last_data_date: dt.datetime) -> pd.Series:
        """Uses a fit BATS model to predict between the given dates"""
        in_sample_preds = pd.Series(self.fit_model.y_hat,
                                    index=pd.date_range(start=self.data.index[0],
                                                        end=self.data.index[-1], freq='MS'))

        # start date and end date are both in-sample
        if start_date < in_sample_preds.index[-1] and end_date <= in_sample_preds.index[-1]:
            preds = in_sample_preds.loc[start_date:end_date]

        # start date is in-sample but end date is not
        elif start_date < self.data.index[-1] < end_date:
            num_extra_months = (end_date.year - last_data_date.year) * 12 + (end_date.month - last_data_date.month)
            # get all in sample predictions and stitch them together with out of sample predictions
            in_sample_portion = in_sample_preds.loc[start_date:]
            out_of_sample_portion = self.fit_model.forecast(num_extra_months)
            preds = np.concatenate([in_sample_portion, out_of_sample_portion])

        # only possible scenario at this point is start date is 1 month past last data date
        else:
            months_to_predict = (end_date.year - start_date.year) * 12 + (end_date.month - start_date.month) + 1
            preds = self.fit_model.forecast(months_to_predict)

        return pd.Series(preds, index=pd.date_range(start=start_date, end=end_date, freq='MS'))

    def _predict_ensemble(self, start_date: dt.datetime, end_date: dt.datetime,
                          last_data_date: dt.datetime, exogenous: pd.DataFrame) -> pd.Series:
        """Uses all other fit models to predict between the given dates and averages them"""
        ensemble_model_predictions = []

        if 'auto_arima' in self.model_names:
            # todo: the way this works is kind of janky right now. probably want to move away from setting
            #  and resetting the fit_model attribute for each candidate model
            for candidate in self.candidate_models:
                if candidate.model_type == 'auto_arima':
                    self.fit_model = candidate.fit_model
            preds = self._predict_auto_arima(start_date, end_date, last_data_date, exogenous)
            preds = preds.rename('auto_arima_predictions')
            ensemble_model_predictions.append(preds)

        if 'exponential_smoothing' in self.model_names:
            for candidate in self.candidate_models:
                if candidate.model_type == 'exponential_smoothing':
                    self.fit_model = candidate.fit_model
            preds = self._predict_exponential_smoothing(start_date, end_date)
            preds = preds.rename('exponential_smoothing_predictions')
            ensemble_model_predictions.append(preds)

        if 'tbats' in self.model_names:
            for candidate in self.candidate_models:
                if candidate.model_type == 'tbats':
                    self.fit_model = candidate.fit_model
            preds = self._predict_tbats(start_date, end_date, last_data_date)
            preds = preds.rename('tbats_predictions')
            ensemble_model_predictions.append(preds)

        all_predictions = reduce(lambda left, right: pd.merge(left, right,
                                                              left_index=True, right_index=True),
                                 ensemble_model_predictions)
        all_predictions['en_test_predictions'] = all_predictions.mean(axis='columns')

        self.fit_model = None
        self.fit_model_type = 'ensemble'

        return pd.Series(all_predictions['en_test_predictions'].values,
                         index=pd.date_range(start=start_date, end=end_date, freq='MS'))

    def predict(self, start_date: Union[dt.datetime, str], end_date: Union[dt.datetime, str],
                exogenous: pd.DataFrame = None) -> pd.Series:
        """
        Generates predictions (forecasts) for dates between start_date and end_date (inclusive).
        :param start_date: date to begin forecast (inclusive), must be either within the date range
        given during fit or the month immediately following the last date given during fit
        :param end_date: date to end forecast (inclusive)
        :param exogenous: A dataframe of the exogenous regressor column(s) provided during fit().
        The dataframe should be of equal length to the number of predictions you would like to receive
        :return: A pandas Series of length equal to the number of months between start_date and
        end_date. The series' will have a datetime index
        """
        # checks on data
        if not self.is_fitted:
            raise AttributeError('Model must be fitted to be able to make predictions. Use the '
                                 '`fit` method to fit before predicting')

        # check inputs are datetimes or strings that are capable of being turned into datetimes
        if isinstance(start_date, str):
            start_date = pd.to_datetime(start_date)
        elif not isinstance(start_date, dt.datetime):
            raise TypeError('`start_date` must be a str or datetime-like object')
        if isinstance(end_date, str):
            end_date = pd.to_datetime(end_date)
        elif not isinstance(end_date, dt.datetime):
            raise TypeError('`end_date` must be a str or datetime-like object')

        # check start date doesn't come before end_date
        if start_date > end_date:
            raise ValueError('`end_date` must come after `start_date`')

        # check that start date is before or right after that last date given during training
        last_date = self.data.index[-1]
        if start_date > (last_date + relativedelta(months=+1)):
            raise ValueError(f'`start_date` must be no more than 1 month past the last date of data received'
                             f' during fit". Received `start_date` is '
                             f'{(start_date.year - last_date.year) * 12 + (start_date.month - last_date.month)} '
                             f'months after last date in data {last_date}')

        # check that start date comes after first date in training
        if start_date < self.data.index[0]:
            raise ValueError(f'`start_date` must be later than the earliest date received during fit, '
                             f'{self.data.index[0]}. Received `start_date` = {start_date}')

        # check that, if the user fit models with exogenous regressors, future values are provided
        # if we are predicting any out-of-sample dates
        if self.using_exogenous and last_date < end_date and exogenous is None:
            raise ValueError('Exogenous regressor(s) must be provided as a dataframe since they '
                             'were provided during training')

        # auto_arima requires a dataframe for the exogenous argument. If user provides a series, go
        # ahead and make it a dataframe, just to be nice :)
        if isinstance(exogenous, pd.Series):
            exogenous = pd.DataFrame(exogenous)

        if self.fit_model_type == 'auto_arima':
            return self._predict_auto_arima(start_date, end_date, last_date, exogenous)

        if self.fit_model_type == 'exponential_smoothing':
            return self._predict_exponential_smoothing(start_date, end_date)

        if self.fit_model_type == 'tbats':
            return self._predict_tbats(start_date, end_date, last_date)

        if self.fit_model_type == 'ensemble':
            return self._predict_ensemble(start_date, end_date, last_date, exogenous)
