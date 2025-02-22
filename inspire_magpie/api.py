# -*- coding: utf-8 -*-
#
# This file is part of Inspire-Magpie.
# Copyright (c) 2016 CERN
#
# Inspire-Magpie is a free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for
# more details.

"""API.

.. codeauthor:: Jan Stypka <jan.stypka@cern.ch>
.. codeauthor:: Jan Aage Lavik <jan.age.lavik@cern.ch>
"""

from __future__ import absolute_import, print_function

import os

import numpy as np
import time

from gensim.models import Word2Vec
from keras.callbacks import Callback, ModelCheckpoint

from magpie import MagpieModel
from magpie.config import NB_EPOCHS, BATCH_SIZE
from magpie.evaluation.rank_metrics import mean_reciprocal_rank, r_precision, \
    mean_average_precision, ndcg_at_k, precision_at_k
from magpie.utils import load_from_disk
from magpie.nn.models import cnn

from .config import (
    DATA_DIR,
    LOG_FOLDER,
    NO_OF_LABELS,
    WORD2VEC_PATH,
    SCALER_PATH,
)
from .labels import get_labels
from .errors import WordDoesNotExist

models = dict()


def get_cached_model(corpus):
    """ Get the cached Keras NN model or rebuild it if missed. """
    global models

    if corpus not in models:
        models[corpus] = build_model_for_corpus(corpus)

    return models[corpus]


def build_model_for_corpus(corpus):
    """ Build an appropriate Keras NN model depending on the corpus """
    if corpus == 'keywords':
        keras_model = cnn(embedding_size=100, output_length=10000)
    elif corpus == 'categories':
        keras_model = cnn(embedding_size=100, output_length=14)
    elif corpus == 'experiments':
        keras_model = cnn(embedding_size=100, output_length=500)
    else:
        raise ValueError('The corpus is not valid')

    model_path = os.path.join(DATA_DIR, corpus, 'model.pickle')
    keras_model.load_weights(model_path)

    w2v_model = Word2Vec.load(WORD2VEC_PATH)
    scaler = load_from_disk(SCALER_PATH)
    labels = get_labels(keras_model.output_shape[1])

    model = MagpieModel(
        keras_model=keras_model,
        word2vec_model=w2v_model,
        scaler=scaler,
        labels=labels,
    )

    return model


def get_word_vector(corpus, positive, negative):
    """Get word vector for positive/negative terms for corpus."""
    w2v_model = get_cached_model(corpus).word2vec_model

    # Check that words exist
    for word in positive + negative:
        if word not in w2v_model:
            raise WordDoesNotExist("{0} does not have a representation "
                                   "in the {1} corpus".format(word, corpus))

    return w2v_model.most_similar(positive=positive, negative=negative)


def predict_labels(corpus, text):
    """Predict labels from text for corpus."""
    model = get_cached_model(corpus)
    return model.predict_from_text(text)


def batch_train(train_dir, test_dir=None, nn='cnn', nb_epochs=NB_EPOCHS,
                batch_size=BATCH_SIZE, persist=False, no_of_labels=NO_OF_LABELS,
                verbose=1):
    model = MagpieModel(
        word2vec_model=Word2Vec.load(WORD2VEC_PATH),
        scaler=load_from_disk(SCALER_PATH),
    )

    logger = CustomLogger(nn)
    model_checkpoint = ModelCheckpoint(
        os.path.join(logger.log_dir, 'keras_model'),
        save_best_only=True,
    )

    history = model.batch_train(
        train_dir,
        get_labels(no_of_labels),
        test_dir=test_dir,
        nn_model=nn,
        callbacks=[logger, model_checkpoint],
        batch_size=batch_size,
        nb_epochs=nb_epochs,
        verbose=verbose,
    )

    finish_logging(logger, history, model.keras_model, persist=persist)

    return history, model


def train(train_dir, test_dir=None, nn='cnn', nb_epochs=NB_EPOCHS,
          batch_size=BATCH_SIZE, persist=False, no_of_labels=NO_OF_LABELS,
          verbose=1):
    model = MagpieModel(
        word2vec_model=Word2Vec.load(WORD2VEC_PATH),
        scaler=load_from_disk(SCALER_PATH),
    )

    logger = CustomLogger(nn)
    model_checkpoint = ModelCheckpoint(
        os.path.join(logger.log_dir, 'keras_model'),
        save_best_only=True,
    )

    history = model.train(
        train_dir,
        get_labels(no_of_labels),
        test_dir=test_dir,
        nn_model=nn,
        callbacks=[logger, model_checkpoint],
        batch_size=batch_size,
        nb_epochs=nb_epochs,
        verbose=verbose,
    )

    finish_logging(logger, history, model.keras_model, persist=persist)

    return history, model


def finish_logging(logger, history, keras_model, persist=False):
    """ Save the rest of the logs after finishing optimisation. """
    history.history['map'] = logger.map_list
    history.history['ndcg'] = logger.ndcg_list
    history.history['mrr'] = logger.mrr_list
    history.history['r_prec'] = logger.r_prec_list
    history.history['precision@3'] = logger.p_at_3_list
    history.history['precision@5'] = logger.p_at_5_list

    if persist:
        keras_model.save_weights(os.path.join(logger.log_dir, 'final_model'))

    # Write acc and loss to file
    for metric in ['acc', 'loss']:
        with open(os.path.join(logger.log_dir, metric), 'wb') as f:
            for val in history.history[metric]:
                f.write(str(val) + "\n")


class CustomLogger(Callback):
    """
    A Keras callback logging additional metrics after every epoch
    """
    def __init__(self, nn_type, verbose=True):
        super(CustomLogger, self).__init__()
        self.map_list = []
        self.ndcg_list = []
        self.mrr_list = []
        self.r_prec_list = []
        self.p_at_3_list = []
        self.p_at_5_list = []
        self.verbose = verbose
        self.nn_type = nn_type
        self.log_dir = self.create_log_dir()

    def create_log_dir(self):
        """ Create a directory where all the logs would be stored  """
        dir_name = '{}_{}'.format(self.nn_type, time.strftime('%d%m%H%M%S'))
        log_dir = os.path.join(LOG_FOLDER, dir_name)
        os.mkdir(log_dir)
        return log_dir

    def log_to_file(self, filename, value):
        """ Write a value to the file """
        with open(os.path.join(self.log_dir, filename), 'a') as f:
            f.write(str(value) + "\n")

    def on_train_begin(self, *args, **kwargs):
        """ Create a config file and write down the run parameters """
        with open(os.path.join(self.log_dir, 'config'), 'wb') as f:
            f.write("Model parameters:\n")
            f.write(str(self.params) + "\n\n")
            f.write("Model YAML:\n")
            f.write(self.model.to_yaml())

    def on_epoch_end(self, epoch, logs=None):
        """ Compute custom metrics at the end of the epoch """
        test_data = self.model.validation_data

        if not test_data:
            return

        if type(test_data) == dict:
            y_test = test_data['output']
            x_test = {'input': test_data['input']}
        else:
            x_test, y_test = test_data[:-2], test_data[-2][0]

        y_pred = self.model.predict(x_test)
        y_pred = np.fliplr(y_pred.argsort())
        for i in xrange(len(y_test)):
            y_pred[i] = y_test[i][y_pred[i]]

        map = mean_average_precision(y_pred)
        mrr = mean_reciprocal_rank(y_pred)
        ndcg = np.mean([ndcg_at_k(row, len(row)) for row in y_pred])
        r_prec = np.mean([r_precision(row) for row in y_pred])
        p_at_3 = np.mean([precision_at_k(row, 3) for row in y_pred])
        p_at_5 = np.mean([precision_at_k(row, 5) for row in y_pred])
        val_acc = logs.get('val_acc', -1)
        val_loss = logs.get('val_loss', -1)

        self.map_list.append(map)
        self.mrr_list.append(mrr)
        self.ndcg_list.append(ndcg)
        self.r_prec_list.append(r_prec)
        self.p_at_3_list.append(p_at_3)
        self.p_at_5_list.append(p_at_5)

        log_dictionary = {
            'map': map,
            'mrr': mrr,
            'ndcg': ndcg,
            'r_prec': r_prec,
            'precision@3': p_at_3,
            'precision@5': p_at_5,
            'val_acc': val_acc,
            'val_loss': val_loss
        }

        for metric_name, metric_value in log_dictionary.iteritems():
            self.log_to_file(metric_name, metric_value)

        if self.verbose:
            print('Mean Average Precision: {}'.format(map))
            print('NDCG: {}'.format(ndcg))
            print('Mean Reciprocal Rank: {}'.format(mrr))
            print('R Precision: {}'.format(r_prec))
            print('Precision@3: {}'.format(p_at_3))
            print('Precision@5: {}'.format(p_at_5))
            print('')
