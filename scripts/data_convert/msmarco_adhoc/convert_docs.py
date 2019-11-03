#!/usr/bin/env python
import sys
import json
import argparse

sys.path.append('scripts')
from data_convert.text_proc import *
from data_convert.convert_common import *

parser = argparse.ArgumentParser(description='Convert MSMARCO-adhoc documents.')
parser.add_argument('--input', metavar='input file', help='input file',
                    type=str, required=True)
parser.add_argument('--output', metavar='output file', help='output file',
                    type=str, required=True)
parser.add_argument('--max_doc_size', metavar='max doc size bytes', help='the threshold for the document size, if a document is larger it is truncated',
                    type=int, default=MAX_DOC_SIZE)


args = parser.parse_args()
print(args)

inpFile = FileWrapper(args.input)
outFile = FileWrapper(args.output, 'w')
maxDocSize = args.max_doc_size

stopWords = readStopWords(STOPWORD_FILE, lowerCase=True)
print(stopWords)
nlp = SpacyTextParser(SPACY_MODEL, stopWords, keepOnlyAlphaNum=True, lowerCase=True)

# Input file is a TSV file
ln=0
for line in inpFile:
  ln+=1
  if not line: 
    continue
  line = line[:maxDocSize] # cut documents that are too long!
  fields = line.split('\t')
  if len(fields) != 4:
    print('Misformated line %d ignoring:' % ln)
    print(line.replace('\t', '<field delimiter>'))
    continue

  did, url, title, body = fields

  title_lemmas, title_unlemm = nlp.procText(title)
  body_lemmas, body_unlemm = nlp.procText(body)

  text = title_lemmas + ' ' + body_lemmas
  text = text.strip()
  text_raw = (title.strip() + ' ' + body.strip()).lower()
  doc = {DOCID_FIELD : did,
         TEXT_FIELD_NAME : text,
         TITLE_UNLEMM_FIELD_NAME : title_unlemm,
         'body' : body_unlemm,
         TEXT_RAW_FIELD_NAME : text_raw}
  docStr = json.dumps(doc) + '\n'
  outFile.write(docStr)
  if ln % REPORT_QTY == 0:
    print('Processed %d docs' % ln)

print('Processed %d docs' % ln)

inpFile.close()
outFile.close()
