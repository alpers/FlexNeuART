#!/usr/bin/env python
# Carry out a key-value data conversion
# of a specified entry from an array of JSON entries 
# to a stupid one-entry-per-line format, 
# where each line has the format:
#
# key:value
#
# Note the following:
# 1. The script expects the input JSON to contain an array of dictionaries
# 2. If the user specifies a dictionary id outside the range, we generate the out-of-range string #OOR
# 3. newlines in JSON keys and values will be replaced with spaces
#
import json
import sys
import re

# Replace \n and \r characters with spaces
# This is a copy-pasted function, but I want to keep
# this simple script to be really light-weight without dependencies
def replaceCharsNL(s):
  return re.sub(r'[\n\r]', ' ', s)

if len(sys.argv) != 4:
  print('Usage <input JSON> <entry ID> <output file>')
  sys.exit(1)

inpData=json.load(open(sys.argv[1]))
if type(inpData) != type([]):
  print('Wrong root-level data type, expecting a list')
  sys.exit(1)

entryId = int(sys.argv[2])
outFile = sys.argv[3]

if entryId < 0 or entryId >= len(inpData):
  res='#OOR' 
else:
  resList=[]

  for key, value in inpData[entryId].items():
    key = replaceCharsNL(str(key))
    value = replaceCharsNL(str(value))
    resList.append(f'{key}:{value}')

  res='\n'.join(resList)

with open(outFile, 'w') as f:
  f.write(res)
