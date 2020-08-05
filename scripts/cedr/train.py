#!/usr/bin/env python
#
# This code is based on CEDR: https://github.com/Georgetown-IR-Lab/cedr
# It has some modifications/extensions and it relies on our custom BERT
# library: https://github.com/searchivarius/pytorch-pretrained-BERT-mod
# (c) Georgetown IR lab & Carnegie Mellon University
# It's distributed under the MIT License
# MIT License is compatible with Apache 2 license for the code in this repo.
#
import os
import gc
import sys
import math
import argparse
import torch
import torch.distributed as dist

sys.path.append('.')

import scripts.utils as utils
import scripts.cedr.data as data
import scripts.cedr.model_init_utils as model_init_utils

from scripts.common_eval import METRIC_LIST, readQrelsDict, readRunDict, getEvalResults
from scripts.config import DEVICE_CPU

from tqdm import tqdm
from collections import namedtuple
from multiprocessing import Process

# Important: all the losses should have a reduction type sum!
class MarginRankingLossWrapper:
    @staticmethod
    def name():
        return 'pairwise_margin'

    '''This is a wrapper class for the margin ranking loss.
       It expects that positive/negative scores are arranged in pairs'''

    def __init__(self, margin):
        self.loss = torch.nn.MarginRankingLoss(margin, reduction='sum')

    def compute(self, scores):
        pos_doc_scores = scores[:, 0]
        neg_doc_scores = scores[:, 1]
        ones = torch.ones_like(pos_doc_scores)
        return self.loss.forward(pos_doc_scores, neg_doc_scores, target=ones)


class PairwiseSoftmaxLoss:
    @staticmethod
    def name():
        return 'pairwise_softmax'

    '''This is a wrapper class for the pairwise softmax ranking loss.
       It expects that positive/negative scores are arranged in pairs'''

    def compute(self, scores):
        return torch.sum(1. - scores.softmax(dim=1)[:, 0])  # pairwise softmax


LOSS_FUNC_LIST = [PairwiseSoftmaxLoss.name(), MarginRankingLossWrapper.name()]

TrainParams = namedtuple('TrainParams',
                    ['init_lr', 'init_bert_lr', 'epoch_lr_decay', 'weight_decay',
                     'warmup_pct', 'batch_sync_qty',
                     'batches_per_train_epoch',
                     'batch_size', 'batch_size_val',
                     'max_query_len', 'max_doc_len',
                     'backprop_batch_size',
                     'epoch_qty', 'save_snapshots',
                     'device_name', 'print_grads',
                     'shuffle_train',
                     'use_external_eval', 'eval_metric'])

def avg_model_params(model):
    """Average model parameters across all GPUs."""
    qty = float(dist.get_world_size())
    for prm in model.parameters():
        dist.all_reduce(prm.data, op=torch.distributed.ReduceOp.SUM)
        prm.data /= qty

def clean_memory(device_name):
    print('Clearning memory device:', device_name)
    gc.collect()
    if device_name != DEVICE_CPU:
        with torch.cuda.device(device_name):
            torch.cuda.empty_cache()


def get_lr_desc(optimizer):
    lr_arr = ['LRs:']
    for param_group in optimizer.param_groups:
        lr_arr.append('%.6f' % param_group['lr'])

    return ' '.join(lr_arr)


def train_iteration(model,
                    is_master_proc, device_qty,
                    loss_obj,
                    train_params, max_train_qty,
                    optimizer, scheduler,
                    dataset, train_pairs, qrels):

    clean_memory(train_params.device_name)

    model.train()
    total_loss = 0.
    total_prev_qty = total_qty = 0. # This is a total number of records processed, it can be different from
                                    # the total number of training pairs

    batch_size = train_params.batch_size

    optimizer.zero_grad()

    if train_params.print_grads:
      print('Gradient sums before training')
      for k, v in model.named_parameters():
        print(k, 'None' if v.grad is None else torch.sum(torch.norm(v.grad, dim=-1, p=2)))

    lr_desc = get_lr_desc(optimizer)

    batch_id = 0

    if is_master_proc:
        pbar = tqdm('training', total=max_train_qty, ncols=80, desc=None, leave=False)
    else:
        pbar = None

    for record in data.iter_train_pairs(model, train_params.device_name, dataset, train_pairs, train_params.shuffle_train,
                                        qrels, train_params.backprop_batch_size,
                                        train_params.max_query_len, train_params.max_doc_len):
        scores = model(record['query_tok'],
                       record['query_mask'],
                       record['doc_tok'],
                       record['doc_mask'])
        count = len(record['query_id']) // 2
        scores = scores.reshape(count, 2)
        loss = loss_obj.compute(scores)
        loss.backward()
        total_qty += count

        if train_params.print_grads:
          print(f'Records processed {total_qty} Gradient sums:')
          for k, v in model.named_parameters():
            print(k, 'None' if v.grad is None else torch.sum(torch.norm(v.grad, dim=-1, p=2)))

        total_loss += loss.item()

        if total_qty - total_prev_qty >= batch_size:
            #print(total, 'optimizer step!')
            optimizer.step()
            optimizer.zero_grad()
            total_prev_qty = total_qty

            # Scheduler must make a step in each batch! *AFTER* the optimizer makes an update!
            if scheduler is not None:
                scheduler.step()
                lr_desc = get_lr_desc(optimizer)

            # This must be done in every process, not only in the master process
            if device_qty > 1:
                if batch_id % train_params.batch_sync_qty == 0:
                    avg_model_params(model)

            batch_id += 1

        if pbar is not None:
            pbar.update(count)
            pbar.set_description('%s train loss %.5f' % (lr_desc, total_loss / float(total_qty)) )
        if total_qty >= max_train_qty:
            break

    if pbar is not None:
        pbar.close()

    return total_loss / float(total_qty)


def validate(model, train_params, dataset, run, qrelf, epoch, model_out_dir):

    rerank_run = run_model(model, train_params, dataset, run)
    eval_metric = train_params.eval_metric

    print(f'Evaluating run with QREL file {qrelf} using metric {eval_metric}')

    runf = os.path.join(model_out_dir, f'{epoch}.run')

    return getEvalResults(train_params.use_external_eval,
                          eval_metric,
                          rerank_run, runf, qrelf)


def run_model(model, train_params, dataset, orig_run, desc='valid'):
    rerank_run = {}
    clean_memory(train_params.device_name)
    with torch.no_grad(), tqdm(total=sum(len(r) for r in orig_run.values()), ncols=80, desc=desc, leave=False) as pbar:
        model.eval()
        for records in data.iter_valid_records(model,
                                               train_params.device_name,
                                               dataset, orig_run,
                                               train_params.batch_size_val,
                                               train_params.max_query_len, train_params.max_doc_len):
            scores = model(records['query_tok'],
                           records['query_mask'],
                           records['doc_tok'],
                           records['doc_mask'])
            for qid, did, score in zip(records['query_id'], records['doc_id'], scores):
                rerank_run.setdefault(qid, {})[did] = score.item()
            pbar.update(len(records['query_id']))

    return rerank_run


def do_train(device_qty, master_port, rank, is_master_proc,
              dataset,
              qrels, qrel_file_name,
              train_pairs, valid_run,
              model_out_dir,
              model, loss_obj, train_params):
    if device_qty > 1:
        os.environ['MASTER_ADDR'] = '127.0.0.1'
        os.environ['MASTER_PORT'] = str(master_port)
        dist.init_process_group(utils.PYTORCH_DISTR_BACKEND, rank=rank, world_size=device_qty)

    device_name = train_params.device_name

    if is_master_proc:
        print('Training parameters:')
        print(train_params)
        print('Loss function:', loss_obj.name())

    print('Device name:', device_name)

    model.to(device_name)

    lr = train_params.init_lr
    bert_lr = train_params.init_bert_lr
    epoch_lr_decay = train_params.epoch_lr_decay
    weight_decay = train_params.weight_decay

    top_valid_score = None

    for epoch in range(train_params.epoch_qty):

        params = [(k, v) for k, v in model.named_parameters() if v.requires_grad]
        non_bert_params = {'params': [v for k, v in params if not k.startswith('bert.')]}
        bert_params = {'params': [v for k, v in params if k.startswith('bert.')], 'lr': bert_lr}

        optimizer = torch.optim.AdamW([non_bert_params, bert_params],
                                       lr=lr, weight_decay=weight_decay)

        bpte = train_params.batches_per_train_epoch
        max_train_qty = data.train_item_qty(train_pairs) if bpte <= 0 else bpte * train_params.batch_size

        lr_steps = int(math.ceil(max_train_qty / train_params.batch_size))
        scheduler = None
        if train_params.warmup_pct:
            if is_master_proc:
                print('Using a scheduler with a warm-up for %f steps' % train_params.warmup_pct)
            scheduler = torch.optim.lr_scheduler.OneCycleLR(optimizer,
                                                            total_steps=lr_steps,
                                                            max_lr=[lr, bert_lr],
                                                            anneal_strategy='linear',
                                                            pct_start=train_params.warmup_pct)
        if is_master_proc:
            print('Optimizer', optimizer)

        loss = train_iteration(model=model,
                               is_master_proc=is_master_proc,
                               device_qty=device_qty, loss_obj=loss_obj,
                               train_params=train_params, max_train_qty=max_train_qty,
                               optimizer=optimizer, scheduler=scheduler,
                               dataset=dataset, train_pairs=train_pairs, qrels=qrels)

        # This must be done in every process, not only in the master process
        # But only if we have more than one device!
        if device_qty > 1:
            avg_model_params(model)

        if is_master_proc:
            os.makedirs(model_out_dir, exist_ok=True)

            print(f'train epoch={epoch} loss={loss:.3g} lr={lr:g} bert_lr={bert_lr:g}')
            valid_score = validate(model, train_params, dataset, valid_run, qrel_file_name, epoch, model_out_dir)
            print(f'validation epoch={epoch} score={valid_score:.4g}')
            # Clearing token cache is a necessary evil, or else the saved model file is going to be a bloated beast
            model.tokenize.clear_cache(model)
            if top_valid_score is None or valid_score > top_valid_score:
                top_valid_score = valid_score
                print('new top validation score, saving the whole model')
                torch.save(model, os.path.join(model_out_dir, 'model.best'))

            if train_params.save_snapshots:
                torch.save(model, os.path.join(model_out_dir, f'model.{epoch}'))

        lr *= epoch_lr_decay
        bert_lr *= epoch_lr_decay



def main_cli():
    parser = argparse.ArgumentParser('CEDR model training and validation')

    model_init_utils.add_model_init_basic_args(parser, True)

    parser.add_argument('--datafiles', metavar='data files', help='data files: docs & queries',
                        type=argparse.FileType('rt'), nargs='+', required=True)

    parser.add_argument('--qrels', metavar='QREL file', help='QREL file',
                        type=argparse.FileType('rt'), required=True)

    parser.add_argument('--train_pairs', metavar='paired train data', help='paired train data',
                        type=argparse.FileType('rt'), required=True)

    parser.add_argument('--valid_run', metavar='validation file', help='validation file',
                        type=argparse.FileType('rt'), required=True)

    parser.add_argument('--model_out_dir',
                        metavar='model out dir', help='an output directory for the trained model',
                        required=True)

    parser.add_argument('--epoch_qty', metavar='# of epochs', help='# of epochs',
                        type=int, default=10)

    parser.add_argument('--no_cuda', action='store_true')

    parser.add_argument('--warmup_pct', metavar='warm-up fraction',
                        default=None, type=float,
                        help='use a warm-up/cool-down learning-reate schedule')

    parser.add_argument('--device_qty', type=int, metavar='# of device for multi-GPU training',
                        default=1, help='# of GPUs for multi-GPU training')

    parser.add_argument('--batch_sync_qty', metavar='# of batches before model sync',
                        type=int, default=4, help='Model syncronization frequency for multi-GPU trainig in the # of batche')

    parser.add_argument('--master_port', type=int, metavar='pytorch master port',
                        default=None, help='pytorch master port for multi-GPU training')

    parser.add_argument('--print_grads', action='store_true',
                        help='print gradient norms of parameters')

    parser.add_argument('--save_snapshots', action='store_true',
                        help='save model after each epoch')

    parser.add_argument('--seed', metavar='random seed', help='random seed',
                        type=int, default=42)

    parser.add_argument('--loss_margin', metavar='loss margin', help='Margin in the margin loss',
                        type=float, default=1)

    parser.add_argument('--init_lr', metavar='init learn. rate',
                        type=float, default=0.001, help='Initial learning rate for BERT-unrelated parameters')

    parser.add_argument('--init_bert_lr', metavar='init BERT learn. rate',
                        type=float, default=0.00005, help='Initial learning rate for BERT parameters')

    parser.add_argument('--epoch_lr_decay', metavar='epoch LR decay',
                        type=float, default=1.0, help='Per-epoch learning rate decay')

    parser.add_argument('--weight_decay', metavar='weight decay',
                        type=float, default=0.0, help='optimizer weight decay')

    parser.add_argument('--batch_size', metavar='batch size',
                        type=int, default=32, help='batch size')

    parser.add_argument('--batch_size_val', metavar='val batch size',
                        type=int, default=32, help='validation batch size')

    parser.add_argument('--backprop_batch_size', metavar='backprop batch size',
                        type=int, default=12,
                        help='batch size for each backprop step')

    parser.add_argument('--batches_per_train_epoch', metavar='# of rand. batches per epoch',
                        type=int, default=0,
                        help='# of random batches per epoch: 0 tells to use all data')

    parser.add_argument('--max_query_val', metavar='max # of val queries',
                        type=int, default=0,
                        help='max # of validation queries: 0 tells to use all data')

    parser.add_argument('--no_shuffle_train', action='store_true',
                        help='disabling shuffling of training data')

    parser.add_argument('--use_external_eval', action='store_true',
                        help='use external eval tools: gdeval or trec_eval')

    parser.add_argument('--eval_metric', choices=METRIC_LIST, default=METRIC_LIST[0],
                        help='Metric list: ' +  ','.join(METRIC_LIST), 
                        metavar='eval metric')

    parser.add_argument('--loss_func', choices=LOSS_FUNC_LIST,
                        default=PairwiseSoftmaxLoss.name(),
                        help='Loss functions: ' + ','.join(LOSS_FUNC_LIST))

    args = parser.parse_args()

    utils.set_all_seeds(args.seed)

    loss_name = args.loss_func
    if loss_name == PairwiseSoftmaxLoss.name():
        loss_obj = PairwiseSoftmaxLoss()
    elif loss_name == MarginRankingLossWrapper.name():
        loss_obj = MarginRankingLossWrapper(margin = args.loss_margin)
    else:
        print('Unsupported loss: ' + loss_name)
        sys.exit(1)

    # If we have the complete model, we just load it,
    # otherwise we first create a model and load *SOME* of its weights.
    # For example, if we start from an original BERT model, which has
    # no extra heads, it we will load only the respective weights and
    # initialize the weights of the head randomly.
    if args.init_model is not None:
        print('Loading a complete model from:', args.init_model.name)
        model = torch.load(args.init_model.name, map_location='cpu')
    elif args.init_model_weights is not None:
        model = model_init_utils.create_model_from_args(args)
        print('Loading model weights from:', args.init_model_weights.name)
        model.load_state_dict(torch.load(args.init_model_weights.name, map_location='cpu'), strict=False)
    else:
        print('Creating the model from scratch!')
        model = model_init_utils.create_model_from_args(args)

    os.makedirs(args.model_out_dir, exist_ok=True)
    print(model)
    model.set_grad_checkpoint_param(args.grad_checkpoint_param)

    dataset = data.read_datafiles(args.datafiles)
    qrelf = args.qrels.name
    qrels = readQrelsDict(qrelf)
    train_pairs_all = data.read_pairs_dict(args.train_pairs)
    valid_run = readRunDict(args.valid_run.name)
    max_query_val = args.max_query_val
    query_ids = list(valid_run.keys())
    if max_query_val > 0:
        query_ids = query_ids[0:max_query_val]
        valid_run = {k: valid_run[k] for k in query_ids}

    print('# of eval. queries:', len(query_ids), ' in the file', args.valid_run.name)


    device_qty = args.device_qty
    master_port = args.master_port
    if device_qty > 1:
        if master_port is None:
            print('Specify a master port for distributed training!')
            sys.exit(1)

    processes = []

    is_distr_train = device_qty > 1

    qids = []

    if is_distr_train:
        qids = list(train_pairs_all.keys())

    # We must go in the reverse direction, b/c
    # rank == 0 trainer is in the same process and
    # we call the function do_train in the same process,
    # i.e., this call is blocking processing and
    # prevents other processes from starting.
    for rank in range(device_qty - 1, -1, -1):
        if is_distr_train:
            device_name = f'cuda:{rank}'
        else:
            device_name = args.device_name
            if args.no_cuda:
                device_name = DEVICE_CPU

        # When we have only a single GPP, the main process is its own master
        is_master_proc = rank == 0

        train_params = TrainParams(init_lr=args.init_lr, init_bert_lr=args.init_bert_lr,
                                    warmup_pct=args.warmup_pct, batch_sync_qty=args.batch_sync_qty,
                                    epoch_lr_decay=args.epoch_lr_decay, weight_decay=args.weight_decay,
                                    backprop_batch_size=args.backprop_batch_size,
                                    batches_per_train_epoch=args.batches_per_train_epoch,
                                    save_snapshots=args.save_snapshots,
                                    batch_size=args.batch_size, batch_size_val=args.batch_size_val,
                                    max_query_len=args.max_query_len, max_doc_len=args.max_doc_len,
                                    epoch_qty=args.epoch_qty, device_name=device_name,
                                    use_external_eval=args.use_external_eval, eval_metric=args.eval_metric.lower(),
                                    print_grads=args.print_grads,
                                    shuffle_train=not args.no_shuffle_train)

        train_pair_qty = len(train_pairs_all)
        if is_distr_train or train_pair_qty < device_qty:
            tpart_qty = int((train_pair_qty + device_qty - 1) / device_qty)
            train_start = rank * tpart_qty
            train_end = min(train_start + tpart_qty, len(qids))
            train_pairs = { k : train_pairs_all[k] for k in qids[train_start : train_end] }
        else:
            train_pairs = train_pairs_all
        print('Process rank %d device %s using %d training pairs out of %d' %
              (rank, device_name, len(train_pairs), train_pair_qty))

        param_dict = {
            'device_qty' : device_qty, 'master_port' : master_port,
             'rank' : rank, 'is_master_proc' : is_master_proc,
             'dataset' : dataset,
             'qrels' : qrels, 'qrel_file_name' : qrelf,
             'train_pairs' : train_pairs, 'valid_run' : valid_run,
             'model_out_dir' : args.model_out_dir,
             'model' : model, 'loss_obj' : loss_obj, 'train_params' : train_params
        }

        if is_distr_train and not is_master_proc:
            p = Process(target=do_train, kwargs=param_dict)
            p.start()
            processes.append(p)
        else:
            do_train(**param_dict)

    for p in processes:
        utils.join_and_check_stat(p)

    if device_qty > 1:
        dist.destroy_process_group()


if __name__ == '__main__':
    # A light-weight subprocessing + this is a must for multi-processing with CUDA
    utils.enable_spawn()
    main_cli()
