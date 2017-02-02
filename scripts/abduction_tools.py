#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

import codecs
from collections import OrderedDict
import json
import logging
import re
from subprocess import call, Popen
import subprocess
import sys

from nltk import Tree

from knowledge import GetLexicalRelationsFromPreds
from normalization import denormalize_token
from semantic_tools import is_theorem_defined
from tactics import get_tactics
from tree_tools import tree_or_string, TreeContains

# Check whether the string "is defined" appears in the output of coq.
# In that case, we return True. Otherwise, we return False.
def IsTheoremDefined(output_lines):
  for output_line in output_lines:
    if len(output_line) > 2 and 'is defined' in (' '.join(output_line[-2:])):
      return True
  return False

def IsTheoremError(output_lines):
  """
  Errors in the construction of a theorem (type mismatches in axioms, etc.)
  are signaled using the symbols ^^^^ indicating where the error is.
  We simply search for that string.
  """
  return any('^^^^' in o for ol in output_lines for o in ol)

def IsIncompleteProof(coq_output_lines):
  for line in coq_output_lines:
    if line == 'Error: Attempt to save an incomplete proof':
      return True
  return False

def FindFinalSubgoalLineIndex(coq_output_lines):
  indices = [i for i, line in enumerate(coq_output_lines)\
               if line.endswith('subgoal')]
  if not indices:
    return None
  return indices[-1]

def FindFinalConclusionSepLineIndex(coq_output_lines):
  indices = [i for i, line in enumerate(coq_output_lines)\
               if line.startswith('===') and line.endswith('===')]
  if not indices:
    return None
  return indices[-1]

def GetPremiseLines_(coq_output_lines):
  premise_lines = []
  line_index_last_subgoal = FindFinalSubgoalLineIndex(coq_output_lines)
  if not line_index_last_subgoal:
    return premise_lines
  subgoal_lines = 0
  for line in coq_output_lines[line_index_last_subgoal+1:]:
    if line.startswith('===') and line.endswith('==='):
      return premise_lines
    if line != "":
      premise_lines.append(line)
  return premise_lines

def GetPremiseLines(coq_output_lines):
  premise_lines = []
  line_index_last_conclusion_sep = FindFinalConclusionSepLineIndex(coq_output_lines)
  if not line_index_last_conclusion_sep:
    return premise_lines
  for line in coq_output_lines[line_index_last_conclusion_sep-1:0:-1]:
    if line == "":
      return premise_lines
    else:
      premise_lines.append(line)
  return premise_lines

def GetConclusionLine(coq_output_lines):
  line_index_last_conclusion_sep = FindFinalConclusionSepLineIndex(coq_output_lines)
  if not line_index_last_conclusion_sep:
    return None
  return coq_output_lines[line_index_last_conclusion_sep + 1]

# This function was used for EACL 2017.
def GetPremisesThatMatchConclusionArgs_(premises, conclusion):
  """
  Returns premises where the predicates have at least one argument
  in common with the conclusion.
  """
  conclusion_terms = [c.strip(')(') for c in conclusion.split()]
  conclusion_args = set(conclusion_terms[1:])
  candidate_premises = []
  for premise in premises:
    premise_terms = [p.strip(')(') for p in premise.split()[2:]]
    premise_args = set(premise_terms[1:])
    logging.debug('Conclusion args: ' + str(conclusion_args) + \
                  '\nPremise args: ' + str(premise_args))
    if premise_args.intersection(conclusion_args):
      candidate_premises.append(premise)
  return candidate_premises

def GetPremisesThatMatchConclusionArgs(premises, conclusion):
  """
  Returns premises where the predicates have at least one argument
  in common with the conclusion.
  """
  candidate_premises = []
  conclusion = re.sub(r'\?([0-9]+)', r'?x\1', conclusion)
  conclusion_args = get_tree_pred_args(conclusion, is_conclusion=True)
  if conclusion_args is None:
    return candidate_premises
  for premise_line in premises:
    # Convert anonymous variables of the form ?345 into ?x345.
    premise_line = re.sub(r'\?([0-9]+)', r'?x\1', premise_line)
    premise_args = get_tree_pred_args(premise_line)
    logging.debug('Conclusion args: ' + str(conclusion_args) + \
                  '\nPremise args: ' + str(premise_args))
    if TreeContains(premise_args, conclusion_args):
      candidate_premises.append(premise_line)
  return candidate_premises

def make_failure_log(conclusion_pred, premise_preds, conclusion, premises,
    coq_output_lines=None):
  """
  Produces a dictionary with the following structure:
  {"unproved sub-goal" : "sub-goal_predicate",
   "matching premises" : ["premise1", "premise2", ...],
   "raw sub-goal" : "conclusion",
   "raw premises" : ["raw premise1", "raw premise2", ...]}
  Raw sub-goal and raw premises are the coq lines with the premise
  internal name and its predicates. E.g.
  H : premise (Acc x1)
  Note that this function is not capable of returning all unproved
  sub-goals in coq's stack. We only return the top unproved sub-goal.
  """
  failure_log = OrderedDict()
  conclusion_base = denormalize_token(conclusion_pred)
  failure_log["unproved sub-goal"] = conclusion_base
  premises_base = [denormalize_token(p) for p in premise_preds]
  failure_log["matching premises"] = premises_base
  failure_log["raw sub-goal"] = conclusion
  failure_log["raw premises"] = premises
  premise_preds = []
  for p in premises:
    try:
      pred = p.split()[2]
    except:
      continue
    if pred.startswith('_'):
      premise_preds.append(denormalize_token(pred))
  failure_log["all premises"] = premise_preds
  failure_log["other sub-goals"] = get_subgoals_from_coq_output(
    coq_output_lines, premises)
  return failure_log

def get_subgoals_from_coq_output(coq_output_lines, premises):
  """
  When the proving is halted due to unprovable sub-goals,
  Coq produces an output similar to this:

  2 subgoals
  
    H1 : True
    H4 : True
    x1 : Event
    H6 : True
    H3 : _play x1
    H : _two (Subj x1)
    H2 : _man (Subj x1)
    H0 : _table (Acc x1)
    H5 : _tennis (Acc x1)
    ============================
     _ping (Acc x1)

  subgoal 2 is:
    _pong (Acc x1)

  This function returns the remaining sub-goals ("_pong" in this example).
  """
  subgoals = []
  subgoal_index = -1
  for line in coq_output_lines:
    if line.strip() == '':
      continue
    line_tokens = line.split()
    if subgoal_index > 0:
      subgoal_line = line
      subgoal_tokens = subgoal_line.split()
      subgoal_pred = subgoal_tokens[0]
      if subgoal_index in [s['index'] for s in subgoals]:
        # This sub-goal has already appeared and is recorded.
        subgoal_index = -1
        continue
      subgoal = {
        'predicate' : denormalize_token(line_tokens[0]),
        'index' : subgoal_index,
        'raw' : subgoal_line}
      matching_premises = GetPremisesThatMatchConclusionArgs(premises, subgoal_line)
      subgoal['matching raw premises'] = matching_premises
      premise_preds = [
        denormalize_token(premise.split()[2]) for premise in matching_premises]
      subgoal['matching premises'] = premise_preds
      subgoals.append(subgoal)
      subgoal_index = -1
    if len(line_tokens) >= 3 and line_tokens[0] == 'subgoal' and line_tokens[2] == 'is:':
      subgoal_index = int(line_tokens[1])
  return subgoals

def MakeAxiomsFromPremisesAndConclusion(premises, conclusion, coq_output_lines=None):
  matching_premises = GetPremisesThatMatchConclusionArgs(premises, conclusion)
  premise_preds = [premise.split()[2] for premise in matching_premises]
  conclusion_pred = conclusion.split()[0]
  pred_args = GetPredicateArguments(premises, conclusion)
  axioms = MakeAxiomsFromPreds(premise_preds, conclusion_pred, pred_args)
  if not axioms and 'False' not in conclusion_pred:
    failure_log = make_failure_log(
      conclusion_pred, premise_preds, conclusion, premises, coq_output_lines)
    print(json.dumps(failure_log), file=sys.stderr)
  return axioms

def parse_coq_line(coq_line):
  try:
    tree_args = tree_or_string('(' + coq_line + ')')
  except ValueError:
    tree_args = None
  return tree_args

def get_tree_pred_args(line, is_conclusion=False):
  """
  Given the string representation of a premise, where each premise is:
    pX : predicate1 (arg1 arg2 arg3)
    pY : predicate2 arg1
  or the conclusion, which is of the form:
    predicate3 (arg2 arg4)
  returns a tree or a string with the arguments of the predicate.
  """
  tree_args = None
  if not is_conclusion:
    tree_args = parse_coq_line(' '.join(line.split()[2:]))
  else:
    tree_args = parse_coq_line(line)
  if tree_args is None or len(tree_args) < 1:
    return None
  return tree_args[0]

def GetPredicateArguments(premises, conclusion):
  """
  Given the string representations of the premises, where each premises is:
    pX : predicate1 arg1 arg2 arg3
  and the conclusion, which is of the form:
    predicate3 arg2 arg4
  returns a dictionary where the key is a predicate, and the value
  is a list of argument names.
  If the same predicate is found with different arguments, then it is
  labeled as a conflicting predicate and removed from the output.
  Conflicting predicates are typically higher-order predicates, such
  as "Prog".
  """
  pred_args = {}
  pred_trees = []
  for premise in premises:
    try:
      pred_trees.append(
        Tree.fromstring('(' + ' '.join(premise.split()[2:]) + ')'))
    except ValueError:
      continue
  try:
    conclusion_tree = Tree.fromstring('(' + conclusion + ')')
  except ValueError:
    return pred_args
  pred_trees.append(conclusion_tree)

  pred_args_list = []
  for t in pred_trees:
    pred = t.label()
    args = t.leaves()
    pred_args_list.append([pred] + args)
  conflicting_predicates = set()
  for pa in pred_args_list:
    pred = pa[0]
    args = pa[1:]
    if pred in pred_args and pred_args[pred] != args:
      conflicting_predicates.add(pred)
    pred_args[pred] = args
  logging.debug('Conflicting predicates: ' + str(conflicting_predicates))
  for conf_pred in conflicting_predicates:
    del pred_args[conf_pred]
  return pred_args

def MakeAxiomsFromPreds(premise_preds, conclusion_pred, pred_args):
  axioms = set()
  linguistic_axioms = \
    GetLexicalRelationsFromPreds(premise_preds, conclusion_pred, pred_args)
  axioms.update(set(linguistic_axioms))
  # if not axioms:
  #   approx_axioms = GetApproxRelationsFromPreds(premise_preds, conclusion_pred)
  #   axioms.update(approx_axioms)
  return axioms

def GetTheoremLine(coq_script_lines):
  for i, line in enumerate(coq_script_lines):
    if line.startswith('Theorem '):
      return i
  assert False, 'There was no theorem defined in the coq script: {0}'\
    .format('\n'.join(coq_script_lines))

def InsertAxiomsInCoqScript(axioms, coq_script):
  coq_script_lines = coq_script.split('\n')
  theorem_line = GetTheoremLine(coq_script_lines)
  for axiom in axioms:
    axiom_name = axiom.split()[1]
    coq_script_lines.insert(theorem_line, 'Hint Resolve {0}.'.format(axiom_name))
    coq_script_lines.insert(theorem_line, axiom)
  new_coq_script = '\n'.join(coq_script_lines)
  return new_coq_script

def TryAbductions_(coq_scripts):
  assert len(coq_scripts) == 2
  direct_proof_script = coq_scripts[0]
  reverse_proof_script = coq_scripts[1]
  axioms = set()
  inference_result_str, direct_proof_scripts, new_axioms = \
    TryAbduction(direct_proof_script, previous_axioms=axioms, expected='yes')
  axioms.update(new_axioms)
  reverse_proof_scripts = []
  if not inference_result_str == 'yes':
    inference_result_str, reverse_proof_scripts, new_axioms = \
      TryAbduction(reverse_proof_script, previous_axioms=axioms, expected='no')
  all_scripts = direct_proof_scripts + reverse_proof_scripts
  return inference_result_str, all_scripts

def TryAbductions(coq_scripts):
  assert len(coq_scripts) == 2
  direct_proof_script = coq_scripts[0]
  reverse_proof_script = coq_scripts[1]
  axioms = set()
  # from pudb import set_trace; set_trace()
  while True:
    inference_result_str, direct_proof_scripts, new_direct_axioms = \
      TryAbduction(direct_proof_script, previous_axioms=axioms, expected='yes')
    current_axioms = axioms.union(new_direct_axioms)
    reverse_proof_scripts = []
    if not inference_result_str == 'yes':
      inference_result_str, reverse_proof_scripts, new_reverse_axioms = \
        TryAbduction(reverse_proof_script, previous_axioms=current_axioms, expected='no')
      current_axioms.update(new_reverse_axioms)
    all_scripts = direct_proof_scripts + reverse_proof_scripts
    if len(axioms) == len(current_axioms) or inference_result_str != 'unknown':
      break
    axioms = current_axioms
  return inference_result_str, all_scripts

def filter_wrong_axioms(axioms, coq_script):
  good_axioms = set()
  for axiom in axioms:
    new_coq_script = InsertAxiomsInCoqScript(set([axiom]), coq_script)
    process = Popen(\
      new_coq_script, \
      shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output_lines = [
      line.decode('utf-8').strip().split() for line in process.stdout.readlines()]
    if not IsTheoremError(output_lines):
      good_axioms.add(axiom)
  return good_axioms

def TryAbduction(coq_script, previous_axioms=set(), expected='yes'):
  new_coq_script = InsertAxiomsInCoqScript(previous_axioms, coq_script)
  current_tactics = get_tactics()
  debug_tactics = 'repeat nltac_base. try substitution. Qed'
  coq_script_debug = new_coq_script.replace(current_tactics, debug_tactics)
  process = Popen(\
    coq_script_debug, \
    shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
  output_lines = [line.decode('utf-8').strip() for line in process.stdout.readlines()]
  if is_theorem_defined(l.split() for l in output_lines):
      return expected, [new_coq_script], previous_axioms
  premise_lines = GetPremiseLines(output_lines)
  conclusion = GetConclusionLine(output_lines)
  if not premise_lines or not conclusion:
    return 'unknown', [], previous_axioms
  matching_premises = GetPremisesThatMatchConclusionArgs(premise_lines, conclusion)
  axioms = MakeAxiomsFromPremisesAndConclusion(premise_lines, conclusion, output_lines)
  axioms = filter_wrong_axioms(axioms, coq_script)
  axioms = axioms.union(previous_axioms)
  new_coq_script = InsertAxiomsInCoqScript(axioms, coq_script)
  process = Popen(\
    new_coq_script, \
    shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
  output_lines = [line.decode('utf-8').strip().split() for line in process.stdout.readlines()]
  inference_result_str = expected if IsTheoremDefined(output_lines) else 'unknown'
  return inference_result_str, [new_coq_script], axioms

