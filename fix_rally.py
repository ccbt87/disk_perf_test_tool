import os
import re
import sys
import time
import yaml
import json
import os.path
import argparse
import datetime
import warnings
import functools
import contextlib
import multiprocessing

from rally import exceptions
from rally.cmd import cliutils
from rally.cmd.main import categories
from rally.benchmark.scenarios.vm.utils import VMScenario

from ssh_copy_directory import put_dir_recursively, ssh_copy_file


def log(x):
    dt_str = datetime.datetime.now().strftime("%H:%M:%S")
    pref = dt_str + " " + str(os.getpid()) + " >>>> "
    sys.stderr.write(pref + x.replace("\n", "\n" + pref) + "\n")


def get_barrier(count):
    val = multiprocessing.Value('i', count)
    cond = multiprocessing.Condition()

    def closure(timeout):
        me_released = False
        with cond:
            val.value -= 1
            if val.value == 0:
                me_released = True
                cond.notify_all()
            else:
                cond.wait(timeout)
            return val.value == 0

        if me_released:
            log("Test begins!")

    return closure


@contextlib.contextmanager
def patch_VMScenario_run_command_over_ssh(paths,
                                          on_result_cb,
                                          barrier=None,
                                          latest_start_time=None):

    orig = VMScenario.run_command_over_ssh

    @functools.wraps(orig)
    def closure(self, ssh, *args, **kwargs):
        try:
            sftp = ssh._client.open_sftp()
        except AttributeError:
            # rally code was changed
            log("Prototype of VMScenario.run_command_over_ssh "
                "was changed. Update patch code.")
            raise exceptions.ScriptError("monkeypatch code fails on "
                                         "ssh._client.open_sftp()")
        try:
            for src, dst in paths.items():
                try:
                    if os.path.isfile(src):
                        ssh_copy_file(sftp, src, dst)
                    elif os.path.isdir(src):
                        put_dir_recursively(sftp, src, dst)
                    else:
                        templ = "Can't copy {0!r} - " + \
                                "it neither file or directory"
                        msg = templ.format(src)
                        log(msg)
                        raise exceptions.ScriptError(msg)
                except exceptions.ScriptError:
                    raise
                except Exception as exc:
                    tmpl = "Scp {0!r} => {1!r} failed - {2!r}"
                    msg = tmpl.format(src, dst, exc)
                    log(msg)
                    raise exceptions.ScriptError(msg)
        finally:
            sftp.close()

        log("Start io test")

        if barrier is not None:
            if latest_start_time is not None:
                timeout = latest_start_time - time.time()
            else:
                timeout = None

            if timeout is not None and timeout > 0:
                msg = "Ready and waiting on barrier. " + \
                      "Will wait at most {0} seconds"
                log(msg.format(int(timeout)))

                if not barrier(timeout):
                    log("Barrier timeouted")

        try:
            code, out, err = orig(self, ssh, *args, **kwargs)
        except Exception as exc:
            log("Rally raises exception {0}".format(exc.message))
            raise

        if 0 != code:
            templ = "Script returns error! code={0}\n {1}"
            log(templ.format(code, err.rstrip()))
        else:
            log("Test finished")
            try:
                try:
                    result = json.loads(out)
                except:
                    pass
                else:
                    if '__meta__' in result:
                        on_result_cb(result)
                        result.pop('__meta__')
                    out = json.dumps(result)
            except Exception as err:
                log("Error during postprocessing results: {0!r}".format(err))

        return code, out, err

    VMScenario.run_command_over_ssh = closure

    try:
        yield
    finally:
        VMScenario.run_command_over_ssh = orig


def run_rally(rally_args):
    return cliutils.run(['rally'] + rally_args, categories)


def prepare_files(testtool_py_argv, dst_testtool_path, files_dir):

    # we do need temporary named files
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        py_file = os.tmpnam()
        yaml_file = os.tmpnam()

    testtool_py_inp_path = os.path.join(files_dir, "io.py")
    py_src_cont = open(testtool_py_inp_path).read()
    args_repl_rr = r'INSERT_TOOL_ARGS\(sys\.argv.*?\)'
    py_dst_cont = re.sub(args_repl_rr, repr(testtool_py_argv), py_src_cont)

    if py_dst_cont == args_repl_rr:
        templ = "Can't find replace marker in file {0}"
        log(templ.format(testtool_py_inp_path))
        exit(1)

    yaml_src_cont = open(os.path.join(files_dir, "io.yaml")).read()
    task_params = yaml.load(yaml_src_cont)
    rcd_params = task_params['VMTasks.boot_runcommand_delete']
    rcd_params[0]['args']['script'] = py_file
    yaml_dst_cont = yaml.dump(task_params)

    open(py_file, "w").write(py_dst_cont)
    open(yaml_file, "w").write(yaml_dst_cont)

    return yaml_file, py_file


def run_test(tool, testtool_py_argv, dst_testtool_path, files_dir):
    path = 'iozone' if 'iozone' == tool else 'fio'
    testtool_local = os.path.join(files_dir, path)

    yaml_file, py_file = prepare_files(testtool_py_argv,
                                       dst_testtool_path,
                                       files_dir)

    config = yaml.load(open(yaml_file).read())

    vm_sec = 'VMTasks.boot_runcommand_delete'
    concurrency = config[vm_sec][0]['runner']['concurrency']

    max_preparation_time = 300

    try:
        copy_files = {testtool_local: dst_testtool_path}

        result_queue = multiprocessing.Queue()
        results_cb = result_queue.put

        do_patch = patch_VMScenario_run_command_over_ssh

        barrier = get_barrier(concurrency)
        max_release_time = time.time() + max_preparation_time

        with do_patch(copy_files, results_cb, barrier, max_release_time):
            log("Start rally with 'task start {0}'".format(yaml_file))
            rally_result = run_rally(['task', 'start', yaml_file])

        # while not result_queue.empty():
        #     log("meta = {0!r}\n".format(result_queue.get()))

        return rally_result

    finally:
        os.unlink(yaml_file)
        os.unlink(py_file)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Run rally disk io performance test")
    parser.add_argument("tool_type", help="test tool type",
                        choices=['iozone', 'fio'])
    parser.add_argument("test_directory", help="directory with test")
    parser.add_argument("-l", dest='extra_logs',
                        action='store_true', default=False,
                        help="print some extra log info")
    parser.add_argument("-o", "--io-opts", dest='io_opts',
                        default=None, help="cmd line options for io.py")
    return parser.parse_args(argv)


def main(argv):
    opts = parse_args(argv)
    dst_testtool_path = '/tmp/io_tool'

    if not opts.extra_logs:
        global log

        def nolog(x):
            pass

        log = nolog

    if opts.io_opts is None:
        testtool_py_argv = ['--type', opts.tool_type,
                            '-a', 'randwrite',
                            '--iodepth', '2',
                            '--blocksize', '4k',
                            '--iosize', '20M',
                            '--binary-path', dst_testtool_path,
                            '-d']
    else:
        testtool_py_argv = opts.io_opts.split(" ")

    run_test(opts.tool_type,
             testtool_py_argv,
             dst_testtool_path,
             opts.test_directory)
    return 0

# ubuntu cloud image
# https://cloud-images.ubuntu.com/trusty/current/trusty-server-cloudimg-amd64-disk1.img

# glance image-create --name 'ubuntu' --disk-format qcow2
# --container-format bare --is-public true --copy-from
# https://cloud-images.ubuntu.com/trusty/current/trusty-server-cloudimg-amd64-disk1.img

if __name__ == '__main__':
    exit(main(sys.argv[1:]))
