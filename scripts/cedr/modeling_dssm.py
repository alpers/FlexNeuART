#
# This code is based on CEDR: https://github.com/Georgetown-IR-Lab/cedr
# (c) Georgetown IR lab, which is distributed under the MIT License.
# MIT License is compatible with Apache 2 license for the code in this repo.
#
from pytools import memoize_method
import torch
import os
import torch.nn.functional as F
import pytorch_pretrained_bert
import modeling_util

#
# TODO: We will at some point need to refactor and extract hard-coded constants to a separate file
#
class BertSepObjEncoder(torch.nn.Module):
  '''
    This class uses a BERT model with an additional fully-connected layer applied
    to the output of the CLS token to produce a dense vector of a given dimensionality.
    It is assumed that we will encode each textual object (query or document) separately.
  '''
  def __init__(self, dim, dropout, l2_normalize=False):
    super().__init__()

    self.BERT_MODEL = 'bert-base-uncased'
    self.l2_normalize = l2_normalize
    self.CHANNELS = 12 + 1  # from bert-base-uncased
    self.BERT_SIZE = 768  # from bert-base-uncased
    self.bert = modeling_util.CustomBertModel.from_pretrained(self.BERT_MODEL)
    # Let's disable BERT training
    if False:
      for param in self.bert.parameters():
        param.requires_grad = False

    self.tokenizer = pytorch_pretrained_bert.BertTokenizer.from_pretrained(self.BERT_MODEL)

    self.dropout = torch.nn.Dropout(dropout)
    # A fully-connected layer that transforms internal BERT representation
    self.fc = torch.nn.Linear(self.BERT_SIZE, dim)
    torch.nn.init.xavier_uniform_(self.fc.weight) 


  def set_grad_checkpoint_param(self, param):
    self.bert.set_grad_checkpoint_param(param)

  def forward(self, toks, mask):
    cls_reps = self.encode_bert(toks, mask)
    out_fc = self.fc(self.dropout(cls_reps))
    if self.l2_normalize:
      out_fc = torch.nn.functional.normalize(out_fc, dim=-1, p=2)
    return out_fc

  def save(self, path):
    state = self.state_dict(keep_vars=True)
    for key in list(state):
      if state[key].requires_grad:
        state[key] = state[key].data
      else:
        del state[key]
    torch.save(state, path)

  def load(self, path):
    self.load_state_dict(torch.load(path), strict=False)

  def encode_bert(self, toks, mask):
    maxlen = self.bert.config.max_position_embeddings

    MAX_TOK_LEN = maxlen - 1 # minus one is for [CLS]

    BATCH, LEN = toks.shape

    if LEN <= 0:
      print('Got empty sequence! Generating zero-vector batch')
      return torch.zeros((BATCH, self.dim))


    subbatch_toks, sbcount = modeling_util.subbatch(toks, MAX_TOK_LEN)
    #print('### ', sbcount, 'toks.shape=', toks.shape, 'subbatch_toks.shape=', subbatch_toks.shape, ' maxlen=', MAX_TOK_LEN)
    subbatch_mask, _ = modeling_util.subbatch(mask, MAX_TOK_LEN)

    CLSS = torch.full_like(subbatch_toks[:, :1], self.tokenizer.vocab['[CLS]'])
    ONES = torch.ones_like(subbatch_mask[:, :1])

    # build BERT input sequences
    toks_4model = torch.cat([CLSS, subbatch_toks], dim=1)
    mask_4model = torch.cat([ONES, subbatch_mask], dim=1)
    # While encoding queries & documents separately we have only one type of the segment
    segment_ids = torch.zeros_like(mask_4model).long()
    # Original CEDR developer Sean MacAvaney's comment: remove padding (will be masked anyway)
    # Leo's comment: Although, we modified the token-merging procedure,
    # it is likely still a useful thing to do.
    toks_4model[toks_4model == -1] = 0

    assert(toks_4model.shape == mask_4model.shape)
    assert(segment_ids.shape == mask_4model.shape)

    results = self.bert(toks_4model, segment_ids, mask_4model, output_all_encoded_layers=False)[-1]
    unsubbatch_results = modeling_util.un_subbatch(results, toks, MAX_TOK_LEN)
    cls_output = unsubbatch_results[:, 0]

    # build aggregate CLS representation by averaging CLS representations within each subbatch

    cls_result = []
    for i in range(cls_output.shape[0] // BATCH):
      cls_result.append(cls_output[i * BATCH:(i + 1) * BATCH])

    return torch.stack(cls_result, dim=2).mean(dim=2)


class DssmBertRanker(torch.nn.Module):
    def __init__(self, dim=128, dropout=0.1):
        super().__init__()
        self.query_encoder = BertSepObjEncoder(dim=dim, dropout=dropout)
        self.doc_encoder = BertSepObjEncoder(dim=dim, dropout=dropout)
        self.tokenizer = self.query_encoder.tokenizer

    # TODO this is a copy-paste of the BertRanker class tokenize
    #      can we somehow unify two classes?
    @memoize_method
    def tokenize(self, text):
      toks = self.tokenizer.tokenize(text)
      toks = [self.tokenizer.vocab[t] for t in toks]
      return toks

    def set_grad_checkpoint_param(self, param):
        self.query_encoder.set_grad_checkpoint_param(param)
        self.doc_encoder.set_grad_checkpoint_param(param)

    def save(self, path):
        self.query_encoder.save(path + '_query')
        self.doc_encoder.save(path + '_doc')

    def load(self, path):
        query_path = path + '_query'
        doc_path = path + '_doc'
        if not os.path.exists(query_path) or not os.path.exists(doc_path):
          print('Trying to load LM-finetuned model')
          self.query_encoder.load(path)
          self.doc_encoder.load(path)
        else:
          self.query_encoder.load(query_path)
          self.doc_encoder.load(doc_path)

    def forward(self, query_tok, query_mask, doc_tok, doc_mask):
      query_vec = self.query_encoder(query_tok, query_mask)
      doc_vec = self.doc_encoder(doc_tok, doc_mask)

      # Returnining batched dot-product
      res = torch.einsum('bi, bi -> b', query_vec, doc_vec)

      return res
