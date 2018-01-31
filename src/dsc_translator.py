#!/usr/bin/env python
__author__ = "Gao Wang"
__copyright__ = "Copyright 2016, Stephens lab"
__email__ = "gaow@uchicago.edu"
__license__ = "MIT"
'''
This file defines methods to translate DSC into pipeline in SoS language
'''
import re, os, sys, msgpack, glob
from xxhash import xxh64
from sos.target import fileMD5, executable
from .utils import OrderedDict, flatten_list, uniq_list, dict2str, convert_null, n2a
__all__ = ['DSC_Translator']

class DSC_Translator:
    '''
    Translate preprocessed DSC to SoS pipelines:
      * Each DSC module's name translates to SoS step name
      * Pipelines are executed via nested SoS workflows
    '''
    def __init__(self, workflows, runtime, replicates = 1, rerun = False, n_cpu = 4, try_catch = False):
        # FIXME: to be replaced by the R utils package
        from .plugin import R_LMERGE, R_SOURCE
        self.output = runtime.output
        self.db = os.path.basename(runtime.output)
        conf_header = 'from dsc.dsc_database import remove_obsolete_output, build_config_db\n'
        job_header = "import msgpack\nfrom collections import OrderedDict\nfrom dsc.utils import n2a\n"\
                     f"parameter: IO_DB = msgpack.unpackb(open('{self.output}/{self.db}.conf.mpk'"\
                     ", 'rb').read(), encoding = 'utf-8', object_pairs_hook = OrderedDict)\n\n"
        processed_steps = dict()
        conf_dict = dict()
        conf_str = []
        job_str = []
        exe_signatures = dict()
        # name map for steps, very important
        # to be used to expand IO_DB after load
        self.step_map = dict()
        # Get workflow steps
        for workflow_id, workflow in enumerate(workflows):
            self.step_map[workflow_id + 1] = dict()
            keys = list(workflow.keys())
            for step in workflow.values():
                flow = "_".join(['_'.join(keys[:keys.index(step.name)]), step.name]).strip('_')
                depend = '_'.join(uniq_list([i[0] for i in step.depends]))
                # either different flow or different dependency will create a new entry
                if (step.name, flow, depend) not in processed_steps:
                    name = (step.name, workflow_id + 1)
                    self.step_map[workflow_id + 1][step.name] = name
                    # Has the core been processed?
                    if len([x for x in [k[0] for k in processed_steps.keys()] if x == step.name]) == 0:
                        job_translator = self.Step_Translator(step, self.db, None, try_catch, replicates)
                        job_str.append(job_translator.dump())
                        job_translator.clean()
                        exe_signatures[step.name] = job_translator.exe_signature
                    processed_steps[(step.name, flow, depend)] = name
                    conf_translator = self.Step_Translator(step, self.db,
                                                           self.step_map[workflow_id + 1],
                                                           try_catch, replicates)
                    conf_dict[name] = conf_translator.dump()
                else:
                    self.step_map[workflow_id + 1][step.name] = processed_steps[(step.name, flow, depend)]
        # Get workflows executions
        io_info_files = []
        self.last_steps = []
        # Execution steps, unfiltered
        self.job_pool = OrderedDict()
        # Do not document steps that has been configured already in its unique context
        configured_steps = set()
        for workflow_id, sequence in enumerate(runtime.sequence):
            sqn = [self.step_map[workflow_id + 1][x] for x in sequence]
            new_steps = [conf_dict[x] for x in sqn if x not in configured_steps]
            configured_steps.update(sqn)
            # Configuration
            if len(new_steps):
                conf_str.append(f"###\n# [{n2a(workflow_id + 1)}]\n###\n" \
                                f"sequence_id = '{workflow_id + 1}'\n"\
                                f'''sequence_name = '{"+".join([n2a(x[1]).lower()+"_"+x[0] for x in sqn])}'\n''' \
                                f"# output: '.sos/.dsc/{self.db}_{workflow_id + 1}.mpk'\n")
                conf_str.extend(new_steps)
                io_info_files.append(f'.sos/.dsc/{self.db}_{workflow_id + 1}.mpk')
            # Execution pool
            ii = 1
            for y in sequence:
                tmp_str = [f"[{n2a(workflow_id + 1).lower()}_{y} ({y})]"]
                tmp_str.append(f"parameter: script_signature = {repr(exe_signatures[y])}")
                if ii > 1:
                    tmp_str.append(f"depends: [sos_step('%s_%s' % (n2a(x[1]).lower(), x[0])) for x in IO_DB['{workflow_id + 1}']['{y}']['depends']]")
                tmp_str.append(f"output: IO_DB['{workflow_id + 1}']['{y}']['output']")
                tmp_str.append(f"sos_run('{y}', {y}_output_files = IO_DB['{workflow_id + 1}']['{y}']['output']"\
                               f", {y}_input_files = IO_DB['{workflow_id + 1}']['{y}']['input'], "\
                               "DSC_STEP_ID_ = '_'.join(script_signature))")
                if ii == len(sequence):
                    self.last_steps.append((y, workflow_id + 1))
                self.job_pool[(y, workflow_id + 1)] = '\n'.join(tmp_str)
                ii += 1
        self.conf_str_py = 'import msgpack\nfrom collections import OrderedDict\n' + \
                           'from dsc.utils import sos_hash_output, sos_group_input, chunks\n' + \
                           '\n'.join([f'## {x}' for x in dict2str(self.step_map).split('\n')]) + \
                           '@profile #via "kernprof -l" and "python -m line_profiler"\ndef prepare_io():\n\t'+ \
                           f'\n\tDSC_UPDATES_ = OrderedDict()\n\t_output = ".sos/.dsc/{self.db}.io.mpk"\n\t' + \
                           '\n\t'.join('\n'.join(conf_str).split('\n')) + \
                           "\n\topen(_output, 'wb').write(msgpack.packb(DSC_UPDATES_))\n\n" + \
                           "prepare_io()"
        self.job_str = job_header + f"DSC_RUTILS = '''{R_SOURCE + R_LMERGE}'''" + "\n{}".format('\n'.join(job_str))
        # tmp_dep = ", ".join([f"sos_step('{n2a(x+1)}')" for x, y in enumerate(set(io_info_files))])
        self.conf_str_sos = conf_header + \
                            "\n[deploy_1 (Hashing output files)]" + \
                            f"\ninput: '.sos/.dsc/{self.db}.prepare.py'\noutput: '.sos/.dsc/{self.db}.io.mpk'" + \
                            "\ntask:\nrun: expand = True\n{} {{_input}}".format(sys.executable) + \
                            "\n[deploy_2 (Removing obsolete output)]" + \
                            f"\nremove_obsolete_output('{self.output}', rerun = {rerun})" + \
                            " \n[deploy_3 (Configuring output filenames)]\n" \
                            f"parameter: vanilla = {rerun}\n"\
                            f"input: '.sos/.dsc/{self.db}.io.mpk'\n"\
                            f"output: '{self.output}/{self.db}.map.mpk', "\
                            f"'{self.output}/{self.db}.conf.mpk'"\
                            "\nbuild_config_db(str(_input[0]), str(_output[0]), "\
                            f"str(_output[1]), vanilla = vanilla, jobs = {n_cpu})"
        #
        self.install_libs(runtime.rlib, "R_library")
        self.install_libs(runtime.pymodule, "Python_Module")

    def write_pipeline(self, pipeline_id, dest = None):
        import tempfile
        res = []
        if pipeline_id == 1:
            res.append(self.conf_str_sos)
            with open(f'.sos/.dsc/{self.db}.prepare.py', 'w') as f:
                f.write(self.conf_str_py)
            open(f'.sos/.dsc/{self.db}.io.meta.mpk', 'wb').write(msgpack.packb(self.step_map))
        else:
            res.append(self.job_str)
        output = dest if dest is not None else (tempfile.NamedTemporaryFile().name + '.sos')
        for item in glob.glob(os.path.join(os.path.dirname(output), "*.sos")):
            os.remove(item)
        with open(output, 'w') as f:
            f.write('\n'.join(res))
        return output

    def filter_execution(self):
        '''Filter steps removing the ones having common input and output'''
        included_steps = []
        for x in self.job_pool:
            if self.step_map[x[1]][x[0]] == x:
                self.job_str += f'\n{self.job_pool[x]}'
                included_steps.append(x)
        #
        self.last_steps = [x for x in self.last_steps if x in included_steps]
        self.job_str += "\n[DSC]\ndepends: {}\noutput: {}".\
                        format(', '.join([f"sos_step('{n2a(x[1]).lower()}_{x[0]}')" for x in self.last_steps]),
                               ', '.join([f"IO_DB['{x[1]}']['{x[0]}']['output']" for x in self.last_steps]))

    def install_libs(self, libs, lib_type):
        from .utils import install_r_lib, install_py_module
        if lib_type not in ["R_library", "Python_Module"]:
            raise ValueError("Invalid library type ``{}``.".format(lib_type))
        if libs is None:
            return
        installed_libs = []
        fn = f'.sos/.dsc/{self.db}.{xxh64("".join(libs)).hexdigest()}.lib-info'
        for item in glob.glob(f'.sos/.dsc/{self.db}.*.lib-info'):
            if item == fn:
                installed_libs = [x.strip() for x in open(fn).readlines() if x.strip().split(' ', 1)[1] in libs]
            else:
                os.remove(item)
        new_libs = []
        for lib in libs:
            if f'{lib_type} {lib}' in installed_libs:
                continue
            else:
                if lib_type == 'R_library':
                    ret = install_r_lib(lib)
                if lib_type == 'Python_Module':
                    ret = install_py_module(lib)
                if ret:
                    new_libs.append(f'{lib_type} {lib}')
        with open(fn, 'w') as f:
            f.write('\n'.join(installed_libs + new_libs))

    class Step_Translator:
        def __init__(self, step, db, step_map, try_catch, replicates):
            '''
            prepare step:
             - will produce source to build config and database for
            parameters and file names. The result is one binary json file (via msgpack)
            with keys "X:Y:Z" where X = DSC sequence ID, Y = DSC subsequence ID, Z = DSC step name
                (name of indexed DSC block corresponding to a computational routine).
            run step:
             - will construct the actual script to run
            '''
            # FIXME
            if len(flatten_list(list(step.rf.values()))) > 1:
                raise ValueError('Multiple output files not implemented')
            self.step_map = step_map
            self.try_catch = try_catch
            self.replicates = replicates
            self.exe_signature = []
            self.prepare = 0 if step_map is None else 1
            self.step = step
            self.current_depends = uniq_list([x[0] for x in step.depends]) if step.depends else []
            self.db = db
            self.input_vars = None
            self.header = ''
            self.loop_string = ['', '']
            self.filter_string = ''
            self.param_string = ''
            self.input_string = ''
            self.output_string = ''
            self.input_option = []
            self.step_option = ''
            self.action = ''
            self.get_header()
            self.get_parameters()
            self.get_input()
            self.get_output()
            self.get_step_option()
            self.get_action()

        def clean(self):
            # Remove temp scripts
            for item in glob.glob(f'.sos/{self.step.name}_*'):
                os.remove(item)

        def get_header(self):
            if self.prepare:
                self.header = f"## Codes for {self.step.name}"
            else:
                self.header = f"[{self.step.name}]\n"
                self.header += f"parameter: DSC_STEP_ID_ = None\nparameter: {self.step.name}_output_files = list"

        def get_parameters(self):
            # Set params, make sure each time the ordering is the same
            self.params = list(self.step.p.keys())
            for key in self.params:
                self.param_string += '{}{} = {}\n'.\
                                     format('' if self.prepare else "parameter: ", key,
                                            repr([convert_null(x, self.step.plugin.name) for x in self.step.p[key]]))
            # if self.replicates > 1:
            #     # FIXME NOT IMPLEMENTED!
            #     self.params.append('DSC_REPLICATE')
            #     self.param_string += f'DSC_REPLICATE = [i+1 for i in range({self.replicates})]'
            if self.params:
                self.loop_string[0] = ' '.join([f'for _{s} in {s}' for s in reversed(self.params)])
            if self.step.ft:
                self.filter_string = ' if ' + self.step.ft

        def get_input(self):
            if self.prepare:
                if self.current_depends:
                    self.input_string += f"## With variables from: {', '.join(self.current_depends)}"
                if len(self.current_depends) >= 2:
                    self.input_vars = f'{n2a(int(self.step_map[self.step.name][1])).lower()}_{self.step.name}_input'
                    self.input_string += '\n{} = sos_group_input({})'.\
                       format(self.input_vars,
                              ', '.join([f'{n2a(int(self.step_map[x][1])).lower()}_{x}_output' for x in self.current_depends]))
                elif len(self.current_depends) == 1:
                    self.input_vars = f"{n2a(int(self.step_map[self.current_depends[0]][1])).lower()}_{self.current_depends[0]}_output"
                else:
                    pass
                if len(self.current_depends):
                    if len(self.current_depends) > 1:
                        self.loop_string[1] = f'for __i in chunks({self.input_vars}, {len(self.current_depends)})'
                    else:
                        self.loop_string[1] = f'for __i in {self.input_vars}'
            else:
                if len(self.current_depends):
                    self.input_string += "parameter: {0}_input_files = list\ninput: dynamic({0}_input_files)".\
                                         format(self.step.name)
                    self.input_option.append(f'group_by = {len(self.current_depends)}')
                else:
                    self.input_string += "input:"
                if len(self.params):
                    if self.filter_string:
                        self.input_option.append("for_each = {{'{0}':[({0}) {1}{2}]}}".\
                                                 format(','.join([f'_{x}' for x in self.params]),
                                                        ' '.join([f'for _{s} in {s}' for s in reversed(self.params)]),
                                                        self.filter_string))
                    else:
                        self.input_option.append(f'for_each = {repr(self.params)}')

        def get_output(self):
            if self.prepare:
                format_string = '.format({})'.format(', '.join([f'_{s}' for s in reversed(self.params)]))
                output_lhs = f"{n2a(int(self.step_map[self.step.name][1])).lower()}_{self.step.name}_output"
                self.output_string += "{3} = sos_hash_output(['{0}'{1} {2}])".\
                                      format(' '.join([self.step.name, str(self.step.exe)] \
                                                      + [f'{x}:{{}}' for x in reversed(self.params)]),
                                             format_string, self.loop_string[0] + self.filter_string, output_lhs)
                if len(self.current_depends):
                    self.output_string += "\n{0} = ['{1}:{{}}:{{}}'.format(item, {3}) " \
                                          "for item in {0} {2}]".format(output_lhs, self.step.name, self.loop_string[1],
                                                                        "':'.join(__i)" if len(self.current_depends) > 1 else "__i")
                else:
                    self.output_string += "\n{0} = ['{1}:{{}}'.format(item) for item in {0}]".\
                                          format(output_lhs, self.step.name)
            else:
                # FIXME
                output_group_by = 1
                self.output_string += f"output: {self.step.name}_output_files, group_by = {output_group_by}"

        def get_step_option(self):
            if not self.prepare and self.step.is_extern:
                self.step_option += "task:\n"

        def get_action(self):
            if self.prepare:
                combined_params = '[([{0}], {1}) {2}]'.\
                                  format(', '.join([f"('exec', '{self.step.exe}')"] \
                                                   + [f"('{x}', _{x})" for x in reversed(self.params)]),
                                         None if self.loop_string[1] is '' else ("'{}'.format(' '.join(__i))" if len(self.current_depends) > 1 else "'{}'.format(__i)"),
                                         ' '.join(self.loop_string) + self.filter_string)
                key = f"DSC_UPDATES_['{self.step.name}:' + str(sequence_id)]"
                self.action += f"{key} = OrderedDict()\n"
                if self.step.depends:
                    self.action += "for x, y in zip({}, {}_{}_output):\n\t{}[' '.join((y, x[1]))]"\
                                  " = dict([('sequence_id', {}), "\
                                  "('sequence_name', {}), ('module', '{}')] + x[0])\n".\
                                  format(combined_params, n2a(int(self.step_map[self.step.name][1])).lower(),
                                         self.step.name, key, 'sequence_id',
                                         'sequence_name', self.step.name)
                else:
                    self.action += "for x, y in zip({}, {}_{}_output):\n\t{}[y]"\
                                   " = dict([('sequence_id', {}), "\
                                   "('sequence_name', {}), ('module', '{}')] + x[0])\n".\
                                   format(combined_params, n2a(int(self.step_map[self.step.name][1])).lower(),
                                          self.step.name, key, 'sequence_id',
                                          'sequence_name', self.step.name)
                self.action += "{0}['DSC_IO_'] = ({1}, {2})\n".\
                               format(key, '[]' if self.input_vars is None else '{0} if {0} is not None else []'.\
                                      format(self.input_vars),
                                      f"{n2a(int(self.step_map[self.step.name][1])).lower()}_{self.step.name}_output")
                # FIXME: multiple output to be implemented
                self.action += "{0}['DSC_EXT_'] = \'{1}\'\n".\
                               format(key, flatten_list(self.step.rf.values())[0])
            else:
                # FIXME: have not considered super-step yet
                # Create fake plugin and command list for now
                for idx, (plugin, cmd) in enumerate(zip([self.step.plugin], [self.step.exe])):
                    self.action += f'task: concurrent = True, workdir = {repr(self.step.workdir)}\n{plugin.name}: expand = "${{ }}"\n'
                    # Add action
                    if not self.step.shell_run:
                        script_begin = plugin.get_input(self.params, len(self.step.depends),
                                                        self.step.libpath, idx,
                                                        cmd.split()[1:] if len(cmd.split()) > 1 else None,
                                                        True if len([x for x in self.step.depends if x[2] == 'var']) else False)
                        script_begin = '{1}\n{0}\n{2}'.\
                                       format(script_begin.strip(),
                                              '## BEGIN code by DSC2',
                                              '## END code by DSC2')
                        if len(self.step.rv):
                            script_end = plugin.get_return(self.step.rv)
                            script_end = '{1}\n{0}\n{2}'.\
                                         format(script_end.strip(),
                                                '## BEGIN code by DSC2',
                                                '## END code by DSC2')
                        else:
                            script_end = ''
                        try:
                            cmd_text = [x.rstrip() for x in open(cmd.split()[0], 'r').readlines()
                                        if x.strip() and not x.strip().startswith('#')]
                        except IOError:
                            raise IOError(f"Cannot find script ``{cmd.split()[0]}``!")
                        if plugin.name == 'R':
                            cmd_text = [f"suppressMessages({x.strip()})"
                                        if re.search(r'^(library|require)\((.*?)\)$', x.strip())
                                        else x for x in cmd_text]
                        script = '\n'.join([script_begin, '\n'.join(cmd_text), script_end])
                        if self.try_catch:
                            script = plugin.add_try(script, len(flatten_list([self.step.rf.values()])))
                        script = f"""## {str(plugin)} script UUID: ${{DSC_STEP_ID_}}\n{script}"""
                        self.action += script
                        self.exe_signature.append(fileMD5(self.step.exe.split()[0], partial = False)
                                                  if os.path.isfile(self.step.exe.split()[0])
                                                  else self.step.exe.split()[0] + \
                                                  (self.step.exe.split()[1]
                                                   if len(self.step.exe.split()) > 1 else ''))
                    else:
                        executable(cmd.split()[0])
                        self.action += cmd

        def dump(self):
            return '\n'.join([x for x in
                              [self.header,
                               self.param_string.strip(),
                               ' '.join([self.input_string,
                                         (', ' if self.input_string != 'input:' else '') + ', '.join(self.input_option)])
                               if not self.prepare else self.input_string,
                               self.output_string,
                               self.step_option,
                               self.action]
                              if x])
