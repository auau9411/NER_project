"""
Usage:
    run.py train TRAIN SENT_VOCAB TAG_VOCAB [options]
    run.py test TEST SENT_VOCAB TAG_VOCAB MODEL [options]
Options:
    --dropout-rate=<float>              dropout rate [default: 0.5]
    --embed-size=<int>                  size of word embedding [default: 256]
    --hidden-size=<int>                 size of hidden state [default: 256]
    --batch-size=<int>                  batch-size [default: 32]
    --max-epoch=<int>                   max epoch [default: 10]
    --clip_max_norm=<float>             clip max norm [default: 5.0]
    --lr=<float>                        learning rate [default: 0.001]
    --log-every=<int>                   log every [default: 10]
    --validation-every=<int>            validation every [default: 250]
    --patience-threshold=<float>        patience threshold [default: 0.98]
    --max-patience=<int>                time of continuous worse performance to decay lr [default: 4]
    --max-decay=<int>                   time of lr decay to early stop [default: 4]
    --lr-decay=<float>                  decay rate of lr [default: 0.5]
    --model-save-path=<file>            model save path [default: ./model/model.pth]
    --optimizer-save-path=<file>        optimizer save path [default: ./model/optimizer.pth]
    --cuda                              use GPU
"""

from docopt import docopt
from vocab import Vocab
import time
import torch
import torch.nn as nn
import bilstm_crf
import utils
import random
import argparse


def train(args):
    """ Training BiLSTMCRF model
    Args:
        args: dict that contains options in command
    """
    sent_vocab = Vocab.load(args['SENT_VOCAB'])
    tag_vocab = Vocab.load(args['TAG_VOCAB'])
    train_data, dev_data = utils.generate_train_dev_dataset(args['TRAIN'], sent_vocab, tag_vocab)
    print('num of training examples: %d' % (len(train_data)))
    print('num of development examples: %d' % (len(dev_data)))

    max_epoch = int(args['--max-epoch'])
    log_every = int(args['--log-every'])
    validation_every = int(args['--validation-every'])
    model_save_path = args['--model-save-path']
    optimizer_save_path = args['--optimizer-save-path']
    min_dev_loss = float('inf')
    # device = torch.device('cuda' if args['--cuda'] else 'cpu')
    print('cuda is available: ', torch.cuda.is_available())
    print('cuda device count: ', torch.cuda.device_count())
    print('cuda device name: ', torch.cuda.get_device_name(0))
    device = torch.device('cuda')
    patience, decay_num = 0, 0

    model = bilstm_crf.BiLSTMCRF(sent_vocab, tag_vocab, float(args['--dropout-rate']), int(args['--embed-size']),
                                 int(args['--hidden-size'])).to(device)
    for name, param in model.named_parameters():
        if 'weight' in name:
            nn.init.normal_(param.data, 0, 0.01)
        else:
            nn.init.constant_(param.data, 0)

    optimizer = torch.optim.Adam(model.parameters(), lr=float(args['--lr']))
    train_iter = 0  # train iter num
    record_loss_sum, record_tgt_word_sum, record_batch_size = 0, 0, 0  # sum in one training log
    cum_loss_sum, cum_tgt_word_sum, cum_batch_size = 0, 0, 0  # sum in one validation log
    record_start, cum_start = time.time(), time.time()

    print('start training...')
    for epoch in range(max_epoch):
        for sentences, tags in utils.batch_iter(train_data, batch_size=int(args['--batch-size'])):
            train_iter += 1
            current_batch_size = len(sentences)
            sentences, sent_lengths = utils.pad(sentences, sent_vocab[sent_vocab.PAD], device)
            tags, _ = utils.pad(tags, tag_vocab[tag_vocab.PAD], device)

            # back propagation
            optimizer.zero_grad()
            batch_loss = model(sentences, tags, sent_lengths)  # shape: (b,)
            loss = batch_loss.mean()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=float(args['--clip_max_norm']))
            optimizer.step()

            record_loss_sum += batch_loss.sum().item()
            record_batch_size += current_batch_size
            record_tgt_word_sum += sum(sent_lengths)

            cum_loss_sum += batch_loss.sum().item()
            cum_batch_size += current_batch_size
            cum_tgt_word_sum += sum(sent_lengths)

            if train_iter % log_every == 0:
                print('log: epoch %d, iter %d, %.1f words/sec, avg_loss %f, time %.1f sec' %
                      (epoch + 1, train_iter, record_tgt_word_sum / (time.time() - record_start),
                       record_loss_sum / record_batch_size, time.time() - record_start))
                record_loss_sum, record_batch_size, record_tgt_word_sum = 0, 0, 0
                record_start = time.time()

            if train_iter % validation_every == 0:
                print('dev: epoch %d, iter %d, %.1f words/sec, avg_loss %f, time %.1f sec' %
                      (epoch + 1, train_iter, cum_tgt_word_sum / (time.time() - cum_start),
                       cum_loss_sum / cum_batch_size, time.time() - cum_start))
                cum_loss_sum, cum_batch_size, cum_tgt_word_sum = 0, 0, 0

                dev_loss = cal_dev_loss(model, dev_data, 64, sent_vocab, tag_vocab, device)
                if dev_loss < min_dev_loss * float(args['--patience-threshold']):
                    min_dev_loss = dev_loss
                    model.save(model_save_path)
                    torch.save(optimizer.state_dict(), optimizer_save_path)
                    patience = 0
                else:
                    patience += 1
                    if patience == int(args['--max-patience']):
                        decay_num += 1
                        if decay_num == int(args['--max-decay']):
                            print('Early stop. Save result model to %s' % model_save_path)
                            return
                        lr = optimizer.param_groups[0]['lr'] * float(args['--lr-decay'])
                        model = bilstm_crf.BiLSTMCRF.load(model_save_path, device)
                        optimizer.load_state_dict(torch.load(optimizer_save_path))
                        for param_group in optimizer.param_groups:
                            param_group['lr'] = lr
                        patience = 0
                print('dev: epoch %d, iter %d, dev_loss %f, patience %d, decay_num %d' %
                      (epoch + 1, train_iter, dev_loss, patience, decay_num))
                cum_start = time.time()
                if train_iter % log_every == 0:
                    record_start = time.time()
    print('Reached %d epochs, Save result model to %s' % (max_epoch, model_save_path))


def test(args):
    """ Testing the model
    Args:
        args: dict that contains options in command
    """
    sent_vocab = Vocab.load(args['SENT_VOCAB'])
    tag_vocab = Vocab.load(args['TAG_VOCAB'])
    sentences, tags = utils.read_corpus(args['TEST'])
    sentences = utils.words2indices(sentences, sent_vocab)
    tags = utils.words2indices(tags, tag_vocab)
    test_data = list(zip(sentences, tags))
    print('num of test samples: %d' % (len(test_data)))

    # device = torch.device('cuda' if args['--cuda'] else 'cpu')
    device = torch.device('cuda:0')
    model = bilstm_crf.BiLSTMCRF.load(args['MODEL'], device)
    print('start testing...')
    print('using device', device)

    start = time.time()
    n_iter, num_words = 0, 0
    tp, fp, fn = 0, 0, 0

    model.eval()
    with torch.no_grad():
        for sentences, tags in utils.batch_iter(test_data, batch_size=int(args['--batch-size']), shuffle=False):
            sentences, sent_lengths = utils.pad(sentences, sent_vocab[sent_vocab.PAD], device)
            predicted_tags = model.predict(sentences, sent_lengths)
            n_iter += 1
            num_words += sum(sent_lengths)
            for tag, predicted_tag in zip(tags, predicted_tags):
                current_tp, current_fp, current_fn = cal_statistics(tag, predicted_tag, tag_vocab)
                tp += current_tp
                fp += current_fp
                fn += current_fn
            if n_iter % int(args['--log-every']) == 0:
                print('log: iter %d, %.1f words/sec, precision %f, recall %f, f1_score %f, time %.1f sec' %
                      (n_iter, num_words / (time.time() - start), tp / (tp + fp), tp / (tp + fn),
                       (2 * tp) / (2 * tp + fp + fn), time.time() - start))
                num_words = 0
                start = time.time()
    print('tp = %d, fp = %d, fn = %d' % (tp, fp, fn))
    precision = tp / (tp + fp)
    recall = tp / (tp + fn)
    f1_score = (2 * tp) / (2 * tp + fp + fn)
    print('Precision: %f, Recall: %f, F1 score: %f' % (precision, recall, f1_score))


def cal_dev_loss(model, dev_data, batch_size, sent_vocab, tag_vocab, device):
    """ Calculate loss on the development data
    Args:
        model: the model being trained
        dev_data: development data
        batch_size: batch size
        sent_vocab: sentence vocab
        tag_vocab: tag vocab
        device: torch.device on which the model is trained
    Returns:
        the average loss on the dev data
    """
    is_training = model.training
    model.eval()
    loss, n_sentences = 0, 0
    with torch.no_grad():
        for sentences, tags in utils.batch_iter(dev_data, batch_size, shuffle=False):
            sentences, sent_lengths = utils.pad(sentences, sent_vocab[sent_vocab.PAD], device)
            tags, _ = utils.pad(tags, tag_vocab[sent_vocab.PAD], device)
            batch_loss = model(sentences, tags, sent_lengths)  # shape: (b,)
            loss += batch_loss.sum().item()
            n_sentences += len(sentences)
    model.train(is_training)
    return loss / n_sentences


def cal_statistics(tag, predicted_tag, tag_vocab):
    """ Calculate TN, FN, FP for the given true tag and predicted tag.
    Args:
        tag (list[int]): true tag
        predicted_tag (list[int]): predicted tag
        tag_vocab: tag vocab
    Returns:
        tp: true positive
        fp: false positive
        fn: false negative
    """
    tp, fp, fn = 0, 0, 0

    def func(tag1, tag2):
        a, b, i = 0, 0, 0
        while i < len(tag1):
            if tag1[i] == tag_vocab['O']:
                i += 1
                continue
            begin, end = i, i
            while end + 1 < len(tag1) and tag1[end + 1] != tag_vocab['O']:
                end += 1
            equal = True
            for j in range(max(0, begin - 1), min(len(tag1), end + 2)):
                if tag1[j] != tag2[j]:
                    equal = False
                    break
            a, b = a + equal, b + 1 - equal
            i = end + 1
        return a, b
    t, f = func(tag, predicted_tag)
    tp += t
    fn += f
    t, f = func(predicted_tag, tag)
    fp += f
    return tp, fp, fn


def main():
    args = docopt(__doc__)
    random.seed(0)
    torch.manual_seed(0)
    print(args)
    if args['--cuda']:
        torch.cuda.manual_seed(0)
    if args['train']:
        train(args)
    elif args['test']:
        test(args)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument('')
    main()