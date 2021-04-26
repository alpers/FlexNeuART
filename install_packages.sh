#!/bin/bash -e
set -o pipefail

curr_dir=$PWD

source scripts/common_proc.sh
source scripts/config.sh

boolOpts=(\
"h" "help" "print help"
"with_giza" "withGiza" "install MGIZA"
)

parseArguments $@

if [ "$help" = "1" ] ; then
  genUsage ""
  exit 1
fi

pip install --upgrade -r requirements.txt

python -m spacy download en_core_web_sm

# This should be installed after numpy or else it will try to isntall an incompatible version!
cd $curr_dir
if [ -d pytorch-pretrained-BERT-mod ] ; then
  rm -rf pytorch-pretrained-BERT-mod
fi

git clone https://github.com/searchivarius/pytorch-pretrained-BERT-mod
cd pytorch-pretrained-BERT-mod
python setup.py install

cd $curr_dir

if [ "$withGiza" = "1" ] ; then
  cd $curr_dir
  rm -rf mgiza
  git clone https://github.com/moses-smt/mgiza.git
  cd mgiza/mgizapp
  cmake .
  make -j 4
  make install
fi

cd $curr_dir/trec_eval 
make  

echo "All is installed!"
