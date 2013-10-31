#!/usr/bin/env python
# coding: utf-8
#
# Example of synthesizing Halide schedules using OpenTuner.  This program
# expects a compiled version of Halide to exist at ~/Halide or at the location
# specified by --halide-dir.
#
# Halide programs must be modified by:
#  1) Inserting AUTOTUNE_HOOK(Func) directly after the algorithm definition
#     in main()
#  2) Creating a settings file that describes the functions and variables
#     (see apps/halide_blur.settings for an example)
#
# Halide can be found here: https://github.com/halide/Halide
#

import adddeps  # fix sys.path

import argparse
import collections
import hashlib
import itertools
import json
import logging
import math
import os
import re
import subprocess
import tempfile

from cStringIO import StringIO
from fn import _
from pprint import pprint

import opentuner
from opentuner.search.manipulator import ConfigurationManipulator
from opentuner.search.manipulator import EnumParameter
from opentuner.search.manipulator import IntegerParameter
from opentuner.search.manipulator import PowerOfTwoParameter
from opentuner.search.manipulator import PermutationParameter
from opentuner.search.manipulator import BooleanParameter
from opentuner.search.manipulator import ScheduleParameter
from opentuner.search import evolutionarytechniques
from opentuner.search import differentialevolution
from opentuner.search import simplextechniques
from opentuner.search import patternsearch
from opentuner.search import bandittechniques
from opentuner.search import technique


COMPILE_CMD = (
  '{args.cxx} "{cpp}" -o "{bin}" -I "{args.halide_dir}/include" '
  '"{args.halide_dir}/bin/libHalide.a" -ldl -lpthread {args.cxxflags} '
  '-DAUTOTUNE_N="{args.input_size}" -DAUTOTUNE_TRIALS={args.trials} '
  '-DAUTOTUNE_LIMIT={limit}')


log = logging.getLogger('halide')

parser = argparse.ArgumentParser(parents=opentuner.argparsers())
parser.add_argument('source', help='Halide source file annotated with '
                                   'AUTOTUNE_HOOK')
parser.add_argument('--halide-dir', default=os.path.expanduser('~/Halide'),
                    help='Installation directory for Halide')
parser.add_argument('--input-size',
                    help='Input size to test with')
parser.add_argument('--trials', default=3, type=int,
                    help='Number of times to test each schedule')
parser.add_argument('--nesting', default=2, type=int,
                    help='Maximum depth for generated loops')
parser.add_argument('--max-split-factor', default=8, type=int,
                    help='The largest value a single split() can add')
parser.add_argument('--compile-command', default=COMPILE_CMD,
                    help='How to compile generated C++ code')
parser.add_argument('--cxx', default='clang++',
                    help='C++ compiler to use (g++ or clang++)')
parser.add_argument('--cxxflags', default='',
                    help='Extra flags to the C++ compiler')
parser.add_argument('--tmp-dir', default='/run/shm'
                    if os.access('/run/shm', os.W_OK) else '/tmp',
                    help='Where to store generated tests')
parser.add_argument('--settings-file',
                    help='Override location of json encoded settings')
parser.add_argument('--final-cfg-file', default=os.path.expanduser('finalcfg'),
                    help='Where to store the final configuration')
parser.add_argument('--random-test', action='store_true',
                    help='Generate a random configuration and run it')
parser.add_argument('--random-source', action='store_true',
                    help='Generate a random configuration and print source ')
parser.add_argument('--debug-error',
                    help='Stop on errors matching a given string')
parser.add_argument('--limit', type=float, default=30,
                    help='Kill compile + runs taking too long (seconds)')
parser.add_argument('--memory-limit', type=int, default=1024 ** 3,
                    help='Set memory ulimit on unix based systems')
parser.add_argument('--enable-unroll', action='store_true',
                    help='Enable .unroll(...) generation')
parser.add_argument('--enable-store-at', action='store_true',
                    help='Never generate .store_at(...)')
parser.add_argument('--gated-store-reorder', action='store_true',
                    help='Only reorder storage if a special parameter is given')


# class HalideRandomConfig(opentuner.search.technique.SearchTechnique):
#   def desired_configuration(self):
#     '''
#     inject random configs with no compute_at() calls to kickstart the search process
#     '''
#     cfg = self.manipulator.random()
#     for k in cfg.keys():
#       if re.match('.*_compute_level', k):
#         cfg[k] = LoopLevel.INLINE
#     return cfg
#
# technique.register(bandittechniques.AUCBanditMetaTechnique([
#         HalideRandomConfig(),
#         differentialevolution.DifferentialEvolutionAlt(),
#         evolutionarytechniques.UniformGreedyMutation(),
#         evolutionarytechniques.NormalGreedyMutation(mutation_rate=0.3),
#       ], name = "HalideMetaTechnique"))


class HalideTuner(opentuner.measurement.MeasurementInterface):

  def __init__(self, args):
    # args.technique = ['HalideMetaTechnique']
    super(HalideTuner, self).__init__(args, program_name=args.source)
    timing_prefix = open(os.path.join(os.path.dirname(__file__),
                                      'timing_prefix.h')).read()
    self.template = timing_prefix + open(args.source).read()
    if not args.settings_file:
      args.settings_file = os.path.splitext(args.source)[0] + '.settings'
    with open(args.settings_file) as fd:
      self.settings = json.load(fd)
    self.post_dominators = post_dominators(self.settings)
    if not args.input_size:
      args.input_size = self.settings['input_size']
    self.min_time = float('inf')
    self.min_collection_cost = float('inf')

  def compute_order_parameter(self, func):
    name = func['name']
    sched_vars = []
    sched_deps = dict()
    for var in func['vars']:
      sched_vars.append((var, 0))
      for i in xrange(1, self.args.nesting):
        sched_vars.append((var, i))
        sched_deps[(var, i - 1)] = [(var, i)]
    return ScheduleParameter('{0}_compute_order'.format(name), sched_vars,
                             sched_deps)

  def manipulator(self):
    """
    The definition of the manipulator is meant to mimic the Halide::Schedule
    data structure and defines the configuration space to search
    """
    manipulator = HalideConfigurationManipulator(self)
    manipulator.add_parameter(HalideComputeAtScheduleParameter(
      'schedule', self.args, self.settings['functions'], self.post_dominators))
    for func in self.settings['functions']:
      name = func['name']
      manipulator.add_parameter(PermutationParameter(
        '{0}_store_order'.format(name), func['vars']))
      manipulator.add_parameter(
        BooleanParameter('{0}_store_order_enabled'.format(name)))
      manipulator.add_parameter(self.compute_order_parameter(func))
      for var in func['vars']:
        manipulator.add_parameter(PowerOfTwoParameter(
          '{0}_vectorize'.format(name), 1, args.max_split_factor))
        manipulator.add_parameter(PowerOfTwoParameter(
          '{0}_unroll'.format(name), 1, args.max_split_factor))
        manipulator.add_parameter(BooleanParameter(
          '{0}_parallel'.format(name)))
        for nesting in xrange(1, self.args.nesting):
          manipulator.add_parameter(PowerOfTwoParameter(
            '{0}_splitfactor_{1}_{2}'.format(name, nesting, var),
            1, args.max_split_factor))

    return manipulator

  def cfg_to_schedule(self, cfg):
    """
    Produce a Halide schedule from a configuration dictionary
    """
    o = StringIO()
    cnt = 0
    temp_vars = list()
    schedule = ComputeAtStoreAtParser(cfg['schedule'], self.post_dominators)
    compute_at = schedule.compute_at
    store_at = schedule.store_at

    # build list of all used variable names
    var_names = dict()
    var_name_order = dict()
    for func in self.settings['functions']:
      name = func['name']
      compute_order = cfg['{0}_compute_order'.format(name)]
      for var in func['vars']:
        var_names[(name, var, 0)] = var
        for nesting in xrange(1, self.args.nesting):
          split_factor = cfg.get('{0}_splitfactor_{1}_{2}'.format(
            name, nesting, var), 0)
          if split_factor > 1 and (name, var, nesting - 1) in var_names:
            var_names[(name, var, nesting)] = '_{var}{cnt}'.format(
              func=name, var=var, nesting=nesting, cnt=cnt)
            temp_vars.append(var_names[(name, var, nesting)])
          cnt += 1
      var_name_order[name] = [var_names[(name, v, n)] for v, n in compute_order
                              if (name, v, n) in var_names]

    for func in self.settings['functions']:
      name = func['name']
      inner_varname = var_name_order[name][-1]
      vectorize = cfg['{0}_vectorize'.format(name)]
      if args.enable_unroll:
        unroll = cfg['{0}_unroll'.format(name)]
      else:
        unroll = 1

      print >>o, name

      for var in func['vars']:
        lastvarname = None

        # handle all splits
        for nesting in xrange(1, self.args.nesting):
          split_factor = cfg.get('{0}_splitfactor_{1}_{2}'.format(
            name, nesting, var), 0)
          if split_factor <= 1:
            break

          for nesting2 in xrange(nesting + 1, self.args.nesting):
            split_factor2 = cfg.get('{0}_splitfactor_{1}_{2}'.format(
              name, nesting2, var), 0)
            if split_factor2 <= 1:
              break
            split_factor *= split_factor2
          varname = var_names[(name, var, nesting)]
          lastvarname = var_names[(name, var, nesting - 1)]

          if varname == inner_varname:
            split_factor *= unroll
            split_factor *= vectorize

          print >>o, '.split({0}, {0}, {1}, {2})'.format(
            lastvarname, varname, split_factor)

      # drop unused variables and truncate (Halide supports only 10 reorders)
      print >>o, '.reorder({0})'.format(
        ', '.join(reversed(var_name_order[name][:10])))

      # reorder_storage
      store_order_enabled = cfg['{0}_store_order_enabled'.format(name)]
      if store_order_enabled or not args.gated_store_reorder:
        store_order = cfg['{0}_store_order'.format(name)]
        print >>o, '.reorder_storage({0})'.format(', '.join(store_order))

      if unroll > 1:
        print >>o, '.unroll({0}, {1})'.format(
          var_name_order[name][-1], unroll * vectorize)

      if vectorize > 1:
        print >>o, '.vectorize({0}, {1})'.format(
          var_name_order[name][-1], vectorize)

      if (compute_at[name] is not None and
         len(var_name_order[compute_at[name][0]]) >= compute_at[name][1]):
        at_func, at_idx = compute_at[name]
        try:
          at_var = var_name_order[at_func][-at_idx]
          print >>o, '.compute_at({0}, {1})'.format(at_func, at_var)
          if not args.enable_store_at:
            pass  # disabled
          elif store_at[name] is None:
            print >>o, '.store_root()'
          elif store_at[name] != compute_at[name]:
            at_func, at_idx = store_at[name]
            at_var = var_name_order[at_func][-at_idx]
            print >>o, '.store_at({0}, {1})'.format(at_func, at_var)
        except IndexError:
          # this is expected when at_idx is too large
          # TODO: implement a cleaner fix
          pass
      else:
        parallel = cfg['{0}_parallel'.format(name)]
        if parallel:
          print >>o, '.parallel({0})'.format(var_name_order[name][0])
        print >>o, '.compute_root()'

      print >>o, ';'

    if temp_vars:
      return 'Halide::Var {0};\n{1}'.format(
        ', '.join(temp_vars), o.getvalue())
    else:
      return o.getvalue()

  def schedule_to_source(self, schedule):
    """
    Generate a temporary Halide cpp file with schedule inserted
    """
    def repl_autotune_hook(match):
      return '\n\n%s\n\n_autotune_timing_stub(%s);' % (
        schedule, match.group(1))
    source = re.sub(r'\n\s*AUTOTUNE_HOOK\(\s*([a-zA-Z0-9_]+)\s*\)',
                    repl_autotune_hook, self.template)
    return source

  def run_schedule(self, schedule, limit):
    """
    Generate a temporary Halide cpp file with schedule inserted and run it
    with our timing harness found in timing_prefix.h.
    """
    return self.run_source(self.schedule_to_source(schedule), limit)

  def run_baseline(self):
    """
    Generate a temporary Halide cpp file with schedule inserted and run it
    with our timing harness found in timing_prefix.h.
    """
    def repl_autotune_hook(match):
      return '\n\n_autotune_timing_stub(%s);' % match.group(1)
    source = re.sub(r'\n\s*BASELINE_HOOK\(\s*([a-zA-Z0-9_]+)\s*\)',
                    repl_autotune_hook, self.template)
    return self.run_source(source)

  def run_source(self, source, limit=0):
    with tempfile.NamedTemporaryFile(suffix='.cpp', prefix='halide',
                                     dir=args.tmp_dir) as cppfile:
      cppfile.write(source)
      cppfile.flush()
      # binfile = os.path.splitext(cppfile.name)[0] + '.bin'
      binfile = '/tmp/halide.bin'
      cmd = args.compile_command.format(
        cpp=cppfile.name, bin=binfile, args=args,
        limit=math.ceil(limit) if limit < float('inf') else 0)
      compile_result = self.call_program(cmd, limit=args.limit,
                                         memory_limit=args.memory_limit)
      if compile_result['returncode'] != 0:
        log.error('compile failed: %s', compile_result)
        return None

    try:
      result = self.call_program(binfile,
                                 limit=args.limit,
                                 memory_limit=args.memory_limit)
      stdout = result['stdout']
      stderr = result['stderr']
      returncode = result['returncode']

      if result['timeout']:
        log.info('compiler timeout %d (%.2f+%.0f cost)', args.limit,
                 compile_result['time'], args.limit)
        return float('inf')
      elif returncode == 142:
        log.info('program timeout %d (%.2f+%.2f cost)', math.ceil(limit),
                 compile_result['time'], result['time'])
        return None
      elif returncode != 0 or stderr:
        log.error('invalid schedule (returncode=%d): %s', returncode,
                  stderr.strip())
        if args.debug_error is not None and (args.debug_error in stderr
                                             or args.debug_error == ""):
          self.debug_schedule('/tmp/halideerror.cpp', source)
        return None
      else:
        try:
          time = json.loads(stdout)['time']
        except:
          log.exception('error parsing output: %s', result)
          return None
        log.info('success: %.4f (collection cost %.2f + %.2f)',
                 time, compile_result['time'], result['time'])
        self.min_time = min(self.min_time, time)
        self.min_collection_cost = min(
          self.min_collection_cost, result['time'])
        return time
    finally:
      os.unlink(binfile)

  def run_cfg(self, cfg, limit=0):
    try:
      schedule = self.cfg_to_schedule(cfg)
    except:
      log.exception('error generating schedule')
      return None
    return self.run_schedule(schedule, limit)

  def run(self, desired_result, input, limit):
    time = self.run_cfg(desired_result.configuration.data, limit)
    if time is not None:
      return opentuner.resultsdb.models.Result(time=time)
    else:
      return opentuner.resultsdb.models.Result(state='ERROR',
                                               time=float('inf'))

  def save_final_config(self, configuration):
    """called at the end of tuning"""
    print 'Final Configuration:'
    schedule = self.cfg_to_schedule(configuration.data)
    print schedule
    if args.final_cfg_file:
        final_result = {'schedule' : schedule, 'time' : self.min_time}
        open(args.final_cfg_file, 'w').write(json.dumps(final_result))

  def debug_schedule(self, filename, source):
    open(filename, 'w').write(source)
    raw_input('offending schedule written to {0} press ENTER to continue'
              .format(filename))


class ComputeAtStoreAtParser(object):

  """
  A recursive descent parser to force proper loop nesting, and enforce post
  dominator scheduling constraints

  For each function input will have tokens like:
  ('foo', 's') = store_at location for foo
  ('foo', '2'), ('foo', '1') = opening the loop nests for foo,
                               the inner 2 variables
  ('foo', 'c') = the computation of foo, and closing all loop nests

  The order of these tokens define a loop nest tree which we reconstruct
  """

  def __init__(self, tokens, post_dominators):
    self.tokens = list(tokens)  # input, processed back to front
    self.post_dominators = post_dominators
    self.compute_at = dict()
    self.store_at = dict()
    self.process_root()

  def process_root(self):
    old_len = len(self.tokens)
    out = []
    while self.tokens:
      if self.tokens[-1][1] == 's':
        # store at root
        self.store_at[self.tokens[-1][0]] = None
        out.append(self.tokens.pop())
      else:
        self.process_loopnest(out, [])
    self.tokens = list(reversed(out))
    assert old_len == len(self.tokens)

  def process_loopnest(self, out, stack):
    func, idx = self.tokens[-1]
    out.append(self.tokens.pop())
    if idx != 'c':
      raise Exception('Invalid schedule')

    self.compute_at[func] = None
    for targ_func, targ_idx in reversed(stack):
      if targ_func in self.post_dominators[func]:
        self.compute_at[func] = (targ_func, targ_idx)
        break

    close_tokens = [(f, i) for f, i in self.tokens if f == func and i != 's']
    while close_tokens:
      if self.tokens[-1] == close_tokens[-1]:
        # proper nesting
        close_tokens.pop()
        out.append(self.tokens.pop())
      elif self.tokens[-1][1] == 'c':
        self.process_loopnest(out, stack + close_tokens[-1:])
      elif self.tokens[-1][1] == 's':
        # self.tokens[-1] is computed at this level
        if func in self.post_dominators[self.tokens[-1][0]]:
          self.store_at[self.tokens[-1][0]] = close_tokens[-1]
        else:
          self.store_at[self.tokens[-1][0]] = None
        out.append(self.tokens.pop())
      else:
        # improper nesting, just close the loop and search/delete close_tokens
        out.extend(reversed(close_tokens))
        self.tokens = [x for x in self.tokens if x not in close_tokens]
        break


class HalideConfigurationManipulator(ConfigurationManipulator):

  def __init__(self, halide_tuner):
    super(HalideConfigurationManipulator, self).__init__()
    self.halide_tuner = halide_tuner

  def hash_config(self, config):
    """
    Multiple configs can lead to the same schedule, so we provide a custom
    hash function that hashes the resulting schedule instead of the raw config.
    This will lead to fewer duplicate tests.
    """
    self.normalize(config)
    try:
      schedule = self.halide_tuner.cfg_to_schedule(config)
      return hashlib.sha256(schedule).hexdigest()
    except:
      log.warning('error hashing config', exc_info=True)
      return super(HalideConfigurationManipulator, self).hash_config(config)


class HalideComputeAtScheduleParameter(ScheduleParameter):

  def __init__(self, name, args, functions, post_dominators):
    """
    Custom ScheduleParameter that normalizes using ComputeAtStoreAtParser
    """
    super(HalideComputeAtScheduleParameter, self).__init__(
      name, *self.gen_nodes_deps(args, functions))
    self.post_dominators = post_dominators

  def gen_nodes_deps(self, args, functions):
    """
    Compute the list of nodes and point-to-point deps to provide to base class
    """
    nodes = list()
    deps = collections.defaultdict(list)
    for func in functions:
      last = None
      for idx in reversed(['c'] +  # 'c' = compute location (and close loops)
                          range(1, len(func['vars']) * args.nesting + 1) +
                          ['s']):  # 's' = storage location
        name = (func['name'], idx)
        if last is not None:
          # variables must go in order
          deps[last].append(name)
        last = name
        nodes.append(name)
        if idx == 'c':
          # computes must follow call graph order
          for callee in func['calls']:
            deps[(callee, 'c')].append(name)
    return nodes, deps

  def normalize(self, cfg):
    """
    First enforce basic point-to-point deps (in base class), then call
    ComputeAtStoreAtParser to normalize schedule.
    """
    super(HalideComputeAtScheduleParameter, self).normalize(cfg)
    cfg[self.name] = ComputeAtStoreAtParser(cfg[self.name],
                                            self.post_dominators).tokens


def post_dominators(settings):
  """
  Compute post dominator tree using textbook iterative algorithm for the
  call graph defined in settings
  """
  functions = [f['name'] for f in settings['functions']]
  calls = {f['name']: set(f['calls']) for f in settings['functions']}
  invcalls = collections.defaultdict(set)
  for k, callees in calls.items():
    for v in callees:
      invcalls[v].add(k)
  dom = {functions[-1]: set([functions[-1]])}
  for f in functions[:-1]:
    dom[f] = set(functions)
  change = True
  while change:
    change = False
    for f in functions[:-1]:
      old = dom[f]
      dom[f] = set([f]) | reduce(_ & _, [dom[c]
                                         for c in invcalls[f]], set(functions))
      if old != dom[f]:
        change = True
  return dom


def random_test(args):
  """
  Generate and run a random schedule
  """
  from pprint import pprint
  opentuner.tuningrunmain.init_logging()
  m = HalideTuner(args)
  cfg = m.manipulator().random()
  pprint(cfg)
  print
  schedule = m.cfg_to_schedule(cfg)
  print schedule
  print
  print 'Schedule', m.run_schedule(schedule, 30)
  print 'Baseline', m.run_baseline()


def random_source(args):
  """
  Dump the source code of a random schedule
  """
  opentuner.tuningrunmain.init_logging()
  m = HalideTuner(args)
  cfg = m.manipulator().random()
  schedule = m.cfg_to_schedule(cfg)
  source = m.schedule_to_source(schedule)
  print source


if __name__ == '__main__':
  args = parser.parse_args()
  if args.random_test:
    random_test(args)
  elif args.random_source:
    random_source(args)
  else:
    HalideTuner.main(args)
