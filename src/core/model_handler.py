import os
import time
import json

import torch
from torch.utils.data import DataLoader
import torch.backends.cudnn as cudnn

from .model import Model, evaluate_predictions
from .utils.data_utils import prepare_datasets, QADataStream, vectorize_input
from .utils import Timer, DummyLogger, AverageMeter
from .utils import constants as Constants


class ModelHandler(object):
    """High level model_handler that trains/validates/tests the network,
    tracks and logs metrics.
    """
    def __init__(self, config):
        # Evaluation Metrics:
        self._train_loss = AverageMeter()
        self._dev_loss = AverageMeter()
        self._train_metrics = {'Bleu_1': AverageMeter(),
                                'Bleu_2': AverageMeter(),
                                'Bleu_3': AverageMeter(),
                                'Bleu_4': AverageMeter(),
                                # 'METEOR': AverageMeter(),
                                'ROUGE_L': AverageMeter()}
        self._dev_metrics = {'Bleu_1': AverageMeter(),
                                'Bleu_2': AverageMeter(),
                                'Bleu_3': AverageMeter(),
                                'Bleu_4': AverageMeter(),
                                # 'METEOR': AverageMeter(),
                                'ROUGE_L': AverageMeter()}

        self.logger = DummyLogger(config, dirname=config['out_dir'], pretrained=config['pretrained'])
        self.dirname = self.logger.dirname
        if not config['no_cuda'] and torch.cuda.is_available():
            print('[ Using CUDA ]')
            self.device = torch.device('cuda' if config['cuda_id'] < 0 else 'cuda:%d' % config['cuda_id'])
            #cudnn.benchmark = True
            cudnn.deterministic = True
        else:
            self.device = torch.device('cpu')
        config['device'] = self.device

        # Load BERT featrues
        if config['use_bert']:
            from pytorch_pretrained_bert import BertTokenizer
            from pytorch_pretrained_bert.modeling import BertModel
            print('[ Using pretrained BERT features ]')
            bert_tokenizer = BertTokenizer.from_pretrained(config['bert_model'], do_lower_case=True)
            self.bert_model = BertModel.from_pretrained(config['bert_model']).to(self.device)
            config['bert_model'] = self.bert_model
            if not config.get('finetune_bert', None):
                print('[ Fix BERT layers ]')
                self.bert_model.eval()
                for param in self.bert_model.parameters():
                    param.requires_grad = False
            else:
                print('[ Finetune BERT layers ]')
        else:
            bert_tokenizer = None
            self.bert_model = None

        # Prepare datasets
        datasets = prepare_datasets(config)
        train_set = datasets['train']
        dev_set = datasets['dev']
        test_set = datasets['test']

        # Initialize the QA model
        self._n_train_examples = 0
        self.model = Model(config, train_set)
        self.model.network = self.model.network.to(self.device)

        if train_set:
            self.train_loader = QADataStream(train_set, self.model.vocab_model.word_vocab, self.model.vocab_model.edge_vocab, POS_vocab=self.model.vocab_model.POS_vocab, NER_vocab=self.model.vocab_model.NER_vocab, config=config,
                 isShuffle=True, isLoop=True, isSort=True, ext_vocab=config['pointer'], bert_tokenizer=bert_tokenizer)
            self._n_train_batches = self.train_loader.get_num_batch()
        else:
            self.train_loader = None

        if dev_set:
            self.dev_loader = QADataStream(dev_set, self.model.vocab_model.word_vocab, self.model.vocab_model.edge_vocab, POS_vocab=self.model.vocab_model.POS_vocab, NER_vocab=self.model.vocab_model.NER_vocab, config=config,
                 isShuffle=False, isLoop=True, isSort=True, ext_vocab=config['pointer'], bert_tokenizer=bert_tokenizer)
            self._n_dev_batches = self.dev_loader.get_num_batch()
        else:
            self.dev_loader = None

        if test_set:
            self.test_loader = QADataStream(test_set, self.model.vocab_model.word_vocab, self.model.vocab_model.edge_vocab, POS_vocab=self.model.vocab_model.POS_vocab, NER_vocab=self.model.vocab_model.NER_vocab, config=config,
                 isShuffle=False, isLoop=False, isSort=True, batch_size=config['test_batch_size'], ext_vocab=config['pointer'], bert_tokenizer=bert_tokenizer)
            self._n_test_batches = self.test_loader.get_num_batch()
            self._n_test_examples = len(test_set)
        else:
            self.test_loader = None

        self.config = self.model.config
        self.is_test = False

    def train(self):
        if self.train_loader is None or self.dev_loader is None:
            print("No training set or dev set specified -- skipped training.")
            return

        self.is_test = False
        timer = Timer("Train")
        if self.config['pretrained']:
            self._epoch = self._best_epoch = self.model.saved_epoch
        else:
            self._epoch = self._best_epoch = 0


        self._best_metrics = {}
        for k in self._dev_metrics:
            self._best_metrics[k] = self._dev_metrics[k].mean()
        self._reset_metrics()

        while self._stop_condition(self._epoch, self.config['patience']):
            torch.cuda.empty_cache()
            self._epoch += 1
            rl_ratio = self.config['rl_ratio'] if self._epoch >= self.config['rl_start_epoch'] else 0
            print('rl_ratio: {}'.format(rl_ratio))

            print("\n>>> Train Epoch: [{} / {}]".format(self._epoch, self.config['max_epochs']))
            self.logger.write_to_file("\n>>> Train Epoch: [{} / {}]".format(self._epoch, self.config['max_epochs']))
            self._run_epoch(self.train_loader, training=True, rl_ratio=rl_ratio, verbose=self.config['verbose'])
            train_epoch_time = timer.interval("Training Epoch {}".format(self._epoch))
            format_str = "Training Epoch {} -- Loss: {:0.5f}".format(self._epoch, self._train_loss.mean())
            format_str += self.metric_to_str(self._train_metrics)
            self.logger.write_to_file(format_str)
            print(format_str)

            print("\n>>> Dev Epoch: [{} / {}]".format(self._epoch, self.config['max_epochs']))
            self.logger.write_to_file("\n>>> Dev Epoch: [{} / {}]".format(self._epoch, self.config['max_epochs']))
            self._run_epoch(self.dev_loader, training=False, verbose=self.config['verbose'])
            timer.interval("Validation Epoch {}".format(self._epoch))
            format_str = "Validation Epoch {} -- Loss: {:0.5f}".format(self._epoch, self._dev_loss.mean())
            format_str += self.metric_to_str(self._dev_metrics)
            self.logger.write_to_file(format_str)
            print(format_str)

            self.model.scheduler.step(self._dev_metrics[self.config['eary_stop_metric']].mean())
            if self._best_metrics[self.config['eary_stop_metric']] <= self._dev_metrics[self.config['eary_stop_metric']].mean():
                self._best_epoch = self._epoch
                for k in self._dev_metrics:
                    self._best_metrics[k] = self._dev_metrics[k].mean()

                if self.config['save_params']:
                    self.model.save(self.dirname, self._epoch)
                    print('Saved model to {}'.format(self.dirname))
                format_str = "!!! Updated: " + self.best_metric_to_str(self._best_metrics)
                self.logger.write_to_file(format_str)
                print(format_str)

            self._reset_metrics()
            if rl_ratio > 0:
                self.config['rl_ratio'] = min(self.config['max_rl_ratio'], self.config['rl_ratio'] ** self.config['rl_ratio_power'])


        timer.finish()
        self.training_time = timer.total

        print("Finished Training: {}".format(self.dirname))
        print(self.summary())

    def test(self):
        if self.test_loader is None:
            print("No testing set specified -- skipped testing.")
            return


        if self.config['only_test']:
            self.model.save(self.dirname, 100)
            print('Saved model to {}'.format(self.dirname))


        # Restore the best model
        print('Restoring the best model')
        self.model.init_saved_network(self.dirname)
        self.model.network = self.model.network.to(self.device)


        self.is_test = True
        self._reset_metrics()
        timer = Timer("Test")
        for param in self.model.network.parameters():
            param.requires_grad = False

        if self.bert_model is not None:
            for param in self.bert_model.parameters():
                param.requires_grad = False
        print('[ Beam size: {} ]'.format(self.config['beam_size']))
        output, gold = self._run_epoch(self.test_loader, training=False, verbose=0,
                                 out_predictions=self.config['out_predictions'])

        timer.finish()
        # Note: corpus-level BLEU computes micro-average
        metrics = evaluate_predictions(gold, output)
        format_str = "[test] | test_exs = {} | step: [{} / {}]".format(
                self._n_test_examples, self._n_test_batches, self._n_test_batches)
        format_str += self.plain_metric_to_str(metrics)
        print(format_str)
        self.logger.write_to_file(format_str)

        if self.config['out_predictions']:
            out_dir = self.config['out_dir'] if self.config['out_dir'] else self.config['pretrained']
            out_path = os.path.join(out_dir, 'beam_{}_block_ngram_repeat_{}_{}'.format(self.config['beam_size'], self.config['block_ngram_repeat'], Constants._PREDICTION_FILE))
            with open(out_path, 'w') as out_f:
                for line in output:
                    out_f.write(line + '\n')

            with open(os.path.join(out_dir, Constants._REFERENCE_FILE), 'w') as ref_f:
                for line in gold:
                    ref_f.write(line + '\n')
            print('Saved predictions to {}'.format(out_path))

        print("Finished Testing: {}".format(self.dirname))
        self.logger.close()

    def _run_epoch(self, data_loader, training=True, rl_ratio=0, verbose=10, out_predictions=False):
        start_time = time.time()
        mode = "train" if training else ("test" if self.is_test else "dev")
        test_list = []
        if training:
            self.model.optimizer.zero_grad()
        output = []
        gold = []
        some_index = []
        num = 500
        for step in range(data_loader.get_num_batch()):
            #print(step)
            input_batch = data_loader.nextBatch()
            x_batch = vectorize_input(input_batch, self.config, self.bert_model, training=training, device=self.device)
            if not x_batch:
                continue  # When there are no examples in the batch

            forcing_ratio = self._set_forcing_ratio(step) if training else 0
            res = self.model.predict(x_batch, step, forcing_ratio=forcing_ratio, rl_ratio=rl_ratio, update=training, out_predictions=out_predictions, mode=mode)

            loss = res['loss']
            metrics = res['metrics']
            self._update_metrics(loss, metrics, x_batch['batch_size'], training=training)


            if mode == "test":
                bleu = metrics['Bleu_4']
                print(bleu)
                if bleu < 0.01:
                    if num > 0:
                        num -= 1
                        some_index.append(step)


            if training:
                self._n_train_examples += x_batch['batch_size']

            if (verbose > 0) and (step > 0) and (step % verbose == 0):
                summary_str = self.self_report(step, mode)
                self.logger.write_to_file(summary_str)
                print(summary_str)
                print('used_time: {:0.2f}s'.format(time.time() - start_time))

            if mode == 'test' and out_predictions:
                test_list.append(metrics['Bleu_4'])
                output.extend(res['predictions'])
                gold.extend(x_batch['target_src'])
        if mode == "test":
            print(some_index)
        return output, gold

    def self_report(self, step, mode='train'):
        if mode == "train":
            format_str = "[train-{}] step: [{} / {}] | loss = {:0.5f}".format(
                self._epoch, step, self._n_train_batches, self._train_loss.mean())
            format_str += self.metric_to_str(self._train_metrics)
        elif mode == "dev":
            format_str = "[predict-{}] step: [{} / {}] | loss = {:0.5f}".format(
                    self._epoch, step, self._n_dev_batches, self._dev_loss.mean())
            format_str += self.metric_to_str(self._dev_metrics)
        elif mode == "test":
            format_str = "[test] | test_exs = {} | step: [{} / {}]".format(
                    self._n_test_examples, step, self._n_test_batches)
            format_str += self.metric_to_str(self._dev_metrics)
        else:
            raise ValueError('mode = {} not supported.' % mode)
        return format_str

    def plain_metric_to_str(self, metrics):
        format_str = ''
        for k in metrics:
            format_str += ' | {} = {:0.5f}'.format(k.upper(), metrics[k])
        return format_str

    def metric_to_str(self, metrics):
        format_str = ''
        for k in metrics:
            format_str += ' | {} = {:0.5f}'.format(k.upper(), metrics[k].mean())
        return format_str

    def best_metric_to_str(self, metrics):
        format_str = '\n'
        for k in metrics:
            format_str += '{} = {:0.5f}\n'.format(k.upper(), metrics[k])
        return format_str

    def summary(self):
        start = "\n<<<<<<<<<<<<<<<< MODEL SUMMARY >>>>>>>>>>>>>>>> "
        info = "Best epoch = {}; ".format(self._best_epoch) + self.best_metric_to_str(self._best_metrics)
        end = " <<<<<<<<<<<<<<<< MODEL SUMMARY >>>>>>>>>>>>>>>> "
        return "\n".join([start, info, end])

    def _update_metrics(self, loss, metrics, batch_size, training=True):
        if training:
            if loss:
                self._train_loss.update(loss)
            for k in self._train_metrics:
                if not k in metrics:
                    continue
                self._train_metrics[k].update(metrics[k] * 100, batch_size)
        else:
            if loss:
                self._dev_loss.update(loss)
            for k in self._dev_metrics:
                if not k in metrics:
                    continue
                self._dev_metrics[k].update(metrics[k] * 100, batch_size)

    def _reset_metrics(self):
        self._train_loss.reset()
        self._dev_loss.reset()

        for k in self._train_metrics:
            self._train_metrics[k].reset()
        for k in self._dev_metrics:
            self._dev_metrics[k].reset()

    def _stop_condition(self, epoch, patience=10):
        """
        Checks have not exceeded max epochs and has not gone patience epochs without improvement.
        """
        no_improvement = epoch >= self._best_epoch + patience
        exceeded_max_epochs = epoch >= self.config['max_epochs']
        return False if exceeded_max_epochs or no_improvement else True

    def _set_forcing_ratio(self, step):
        if self.config['forcing_decay_type']:
            if self.config['forcing_decay_type'] == 'linear':
                forcing_ratio = max(0, self.config['forcing_ratio'] - self.config['forcing_decay'] * step)
            elif self.config['forcing_decay_type'] == 'exp':
                forcing_ratio = self.config['forcing_ratio'] * (self.config['forcing_decay'] ** step)
            elif self.config['forcing_decay_type'] == 'sigmoid':
                forcing_ratio = self.config['forcing_ratio'] * self.config['forcing_decay'] / (
                      self.config['forcing_decay'] + math.exp(step / self.config['forcing_decay']))
            else:
                raise ValueError('Unrecognized forcing_decay_type: ' + self.config['forcing_decay_type'])
        else:
            forcing_ratio = self.config['forcing_ratio']
        return forcing_ratio
