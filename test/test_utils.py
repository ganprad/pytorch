import sys
import os
import re
import shutil
import random
import tempfile
import textwrap
import unittest
import torch
import torch.nn as nn
from torch import distributed as dist
import torch.utils.data
import torch.cuda
from torch.utils.checkpoint import checkpoint, checkpoint_sequential
import torch.utils.benchmark as benchmark_utils
from torch.utils.distributed_utils import remove_prefix_from_state_dict_if_exists
import torch.hub as hub
from torch.autograd._functions.utils import check_onnx_broadcast
from torch.onnx.symbolic_opset9 import _prepare_onnx_paddings
from torch.testing._internal.common_utils import load_tests, retry, IS_SANDCASTLE, IS_WINDOWS, slowTest
from urllib.error import URLError
import numpy as np

# load_tests from torch.testing._internal.common_utils is used to automatically filter tests for
# sharding on sandcastle. This line silences flake warnings
load_tests = load_tests

HAS_CUDA = torch.cuda.is_available()

from torch.testing._internal.common_utils import TestCase, run_tests


class RandomDatasetMock(object):

    def __getitem__(self, index):
        return torch.tensor([torch.rand(1).item(), random.uniform(0, 1)])

    def __len__(self):
        return 1000


class TestStateDict(TestCase):
    def test_state_dict(self):
        """"""
        model = nn.Sequential(nn.Linear(32, 32),
                              nn.ReLU(),
                              nn.Linear(32, 16),
                              nn.ReLU())
        # Quantize
        torch.quantization.fuse_modules(model, [["0", "1"], ["2", "3"]], inplace=True)
        model = torch.quantization.QuantWrapper(model)
        torch.backends.quantized.engine = "qnnpack"
        model.qconfig = torch.quantization.get_default_qconfig('qnnpack')
        torch.quantization.prepare_qat(model, inplace=True)

        # Setup/Cleanup:
        def setup(backend, rank, world_size):
            os.environ['MASTER_ADDR'] = 'localhost'
            os.environ['MASTER_PORT'] = '29500'
            dist.init_process_group(backend=backend, rank=rank, world_size=world_size)

        def cleanup():
            dist.destroy_process_group()

        # DataParallel model:
        setup("mpi", rank=8, world_size=1)
        dp_model = nn.DataParallel(model)
        dp_state_dict = remove_prefix_from_state_dict_if_exists(dp_model.state_dict(), prefix='module.')
        self.assertEqual(model.state_dict().keys(), dp_state_dict.keys())
        self.assertEqual(model.state_dict()._metadata.keys(), dp_state_dict._metadata.keys())
        cleanup()

        # Distributed DataParallel model:
        setup("mpi", rank=8, world_size=1)
        ddp_model = nn.parallel.DistributedDataParallel(model)
        ddp_state_dict = remove_prefix_from_state_dict_if_exists(ddp_model.state_dict(), prefix='module.')
        self.assertEqual(model.state_dict().keys(), ddp_state_dict.keys())
        self.assertEqual(model.state_dict()._metadata.keys(), dp_state_dict._metadata.keys())
        cleanup()


class TestCheckpoint(TestCase):

    # This runs checkpoint_sequential on each of the nets in
    # module_lists_to_compare, and compares them against the uncheckpointed model.
    # To compare, it checks outputs as well as input gradients and parameter gradients
    def _check_checkpoint_sequential(
        self,
        model,
        module_lists_to_compare,
        num_chunks,
        input,
    ):

        # not checkpointed
        out = model(input)
        out_not_checkpointed = out.detach().clone()
        model.zero_grad()
        out.sum().backward()
        grad_not_checkpointed = {
            name: param.grad.detach().clone()
            for name, param in model.named_parameters()
        }
        input_grad_not_checkpointed = input.grad.detach().clone()
        for model_to_compare in module_lists_to_compare:
            # checkpointed model by passing list of modules
            detached = input.detach()
            detached.requires_grad = True

            # pass list of modules to checkpoint
            out = checkpoint_sequential(model_to_compare, num_chunks, detached)
            out_checkpointed = out.detach().clone()
            model.zero_grad()
            out.sum().backward()
            grad_checkpointed = {
                name: param.grad.detach().clone()
                for name, param in model.named_parameters()
            }
            input_grad_checkpointed = detached.grad.detach().clone()
            # compare outputs as well as the gradients of input and parameters
            self.assertEqual(out_checkpointed, out_not_checkpointed)
            self.assertEqual(input_grad_not_checkpointed, input_grad_checkpointed)
            for name in grad_checkpointed:
                self.assertEqual(grad_checkpointed[name], grad_not_checkpointed[name])

    # Test whether checkpoint is being triggered or not. For this, we check
    # the number of times forward pass happens
    def test_checkpoint_trigger(self):

        class Net(nn.Module):

            def __init__(self):
                super(Net, self).__init__()
                self.counter = 0

            def forward(self, input_var):
                self.counter += 1
                return input_var

        # checkpointed
        modules = [Net() for _ in range(10)]
        for m in modules:
            self.assertEqual(m.counter, 0)
        input_var = torch.randn(3, 4, requires_grad=True)
        out = checkpoint_sequential(modules, 2, input_var)
        for m in modules:
            self.assertEqual(m.counter, 1)
        out.sum().backward()
        for m in modules[:(len(modules) // 2)]:
            self.assertEqual(m.counter, 2)
        for m in modules[(len(modules) // 2):]:
            self.assertEqual(m.counter, 1)

    def test_checkpoint_valid(self):
        model = nn.Sequential(
            nn.Linear(100, 50),
            nn.ReLU(),
            nn.Linear(50, 20),
            nn.ReLU(),
            nn.Linear(20, 5),
            nn.ReLU()
        )

        input_var = torch.randn(1, 100, requires_grad=True)

        # checkpointed
        chunks = 2
        modules = list(model.children())
        out = checkpoint_sequential(modules, chunks, input_var)
        with self.assertRaisesRegex(RuntimeError, "Checkpointing is not compatible"):
            torch.autograd.grad(
                outputs=[out], grad_outputs=[torch.ones(1, 5)], inputs=[input_var], create_graph=True
            )

    def test_checkpoint(self):
        model = nn.Sequential(
            nn.Linear(100, 50),
            nn.ReLU(),
            nn.Linear(50, 20),
            nn.ReLU(),
            nn.Linear(20, 5),
            nn.ReLU()
        )

        # Compare uncheckpointed model with its checkpointed counterparts
        # In addition to running checkpoint_sequential on the nn.Sequential
        # instance, we also run the function on the list of functions within
        # the module.
        self._check_checkpoint_sequential(
            model,
            [list(model.children()), model],
            2,
            torch.randn(1, 100, requires_grad=True)
        )

    def test_checkpoint_module_list(self):
        class ModuleListNet(nn.Module):
            def __init__(self):
                super(ModuleListNet, self).__init__()
                module_list = [
                    nn.Linear(100, 50),
                    nn.ReLU(),
                    nn.Linear(50, 20),
                    nn.ReLU(),
                    nn.Linear(20, 5),
                    nn.ReLU(),
                ]
                self.module_list = nn.ModuleList(module_list)

            def forward(self, input):
                for layer in self.module_list:
                    input = layer(input)
                return input

        model = ModuleListNet()

        # Compare uncheckpointed model with its checkpointed counterparts.
        self._check_checkpoint_sequential(
            model,
            [list(model.module_list.children()), model.module_list],
            2,
            torch.randn(1, 100, requires_grad=True),
        )

    def test_checkpoint_sequential_deprecated_multiple_args(self):
        class Two(nn.Module):
            def forward(self, a, b):
                return a, b

        model = nn.Sequential(Two())
        a = torch.randn(1, 100, requires_grad=True)
        b = torch.randn(1, 100, requires_grad=True)

        with self.assertRaises(TypeError):
            checkpoint_sequential(model, 1, a, b)

    def test_checkpoint_sequential_deprecated_no_args(self):
        class Noop(nn.Module):
            def forward(self):
                pass

        model = nn.Sequential(Noop())

        with self.assertRaises(TypeError):
            checkpoint_sequential(model, 1)

    def test_checkpoint_rng_cpu(self):
        for _ in range(5):
            inp = torch.randn(20000, device='cpu').requires_grad_()
            phase1 = torch.nn.Dropout()
            phase2 = torch.nn.Dropout()

            def run_fn(input):
                return phase2(input)

            state = torch.get_rng_state()

            out = phase1(inp)
            out = checkpoint(run_fn, out)
            out.sum().backward()
            grad_with_checkpointing = inp.grad

            torch.set_rng_state(state)

            inp.grad = None

            out = phase1(inp)
            out = run_fn(out)
            out.sum().backward()
            grad_no_checkpointing = inp.grad

            self.assertEqual(grad_with_checkpointing, grad_no_checkpointing)

    @unittest.skipIf(not HAS_CUDA, 'No CUDA')
    def test_checkpoint_rng_cuda(self):
        for _ in range(5):
            inp = torch.randn(20000, device='cuda').requires_grad_()
            phase1 = torch.nn.Dropout()
            phase2 = torch.nn.Dropout()

            def run_fn(input):
                return phase2(input)

            state = torch.cuda.get_rng_state()

            out = phase1(inp)
            out = checkpoint(run_fn, out)
            out.sum().backward()
            grad_with_checkpointing = inp.grad

            torch.cuda.set_rng_state(state)

            inp.grad = None

            out = phase1(inp)
            out = run_fn(out)
            out.sum().backward()
            grad_no_checkpointing = inp.grad

            self.assertEqual(grad_with_checkpointing, grad_no_checkpointing)

    def test_checkpoint_non_tensor(self):

        def run_fn(tensor1, tensor2):
            if tensor2 is None:
                return tensor1
            return tensor1 + tensor2

        input_var = torch.randn(1, 100, requires_grad=True)
        out = checkpoint(run_fn, input_var, None)
        out.sum().backward()


class TestDataLoader(TestCase):
    def setUp(self):
        self.dataset = torch.randn(5, 3, 3, 2)
        self.batch_size = 3

    def test_random_seed(self):
        def run():
            dataloader = torch.utils.data.DataLoader(RandomDatasetMock(),
                                                     batch_size=2,
                                                     num_workers=4,
                                                     shuffle=True)
            return next(iter(dataloader))

        torch.manual_seed(2018)
        x1 = run()
        torch.manual_seed(2018)
        x2 = run()
        self.assertEqual(x1, x2)

    def test_single_keep(self):
        dataloader = torch.utils.data.DataLoader(self.dataset,
                                                 batch_size=self.batch_size,
                                                 num_workers=0,
                                                 drop_last=False)
        dataiter = iter(dataloader)
        self.assertEqual(len(list(dataiter)), 2)

    def test_single_drop(self):
        dataloader = torch.utils.data.DataLoader(self.dataset,
                                                 batch_size=self.batch_size,
                                                 num_workers=0,
                                                 drop_last=True)
        dataiter = iter(dataloader)
        self.assertEqual(len(list(dataiter)), 1)

    @unittest.skip("FIXME: Intermittent CUDA out-of-memory error on Windows and time-out under ASAN")
    def test_multi_keep(self):
        dataloader = torch.utils.data.DataLoader(self.dataset,
                                                 batch_size=self.batch_size,
                                                 num_workers=2,
                                                 drop_last=False)
        dataiter = iter(dataloader)
        self.assertEqual(len(list(dataiter)), 2)

    def test_multi_drop(self):
        dataloader = torch.utils.data.DataLoader(self.dataset,
                                                 batch_size=self.batch_size,
                                                 num_workers=2,
                                                 drop_last=True)
        dataiter = iter(dataloader)
        self.assertEqual(len(list(dataiter)), 1)


test_dir = os.path.abspath(os.path.dirname(str(__file__)))


class TestFFI(TestCase):
    def test_deprecated(self):
        with self.assertRaisesRegex(ImportError, "torch.utils.ffi is deprecated. Please use cpp extensions instead."):
            from torch.utils.ffi import create_extension  # noqa: F401


@unittest.skipIf('SKIP_TEST_BOTTLENECK' in os.environ.keys(), 'SKIP_TEST_BOTTLENECK is set')
class TestBottleneck(TestCase):
    def _run(self, command, timeout=30):
        """Returns (return-code, stdout, stderr)"""
        import subprocess

        p = subprocess.Popen(command, stdout=subprocess.PIPE,  # noqa
                             stderr=subprocess.PIPE, shell=True)
        try:
            output, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            output, err = p.communicate()
        rc = p.returncode
        output = output.decode("ascii")
        err = err.decode("ascii")
        return (rc, output, err)

    def _run_bottleneck(self, test_file, scriptargs=''):
        curdir = os.path.dirname(os.path.abspath(__file__))
        filepath = '{}/{}'.format(curdir, test_file)
        if scriptargs != '':
            scriptargs = ' {}'.format(scriptargs)
        rc, out, err = self._run(
            '{} -m torch.utils.bottleneck {}{}'.format(sys.executable, filepath, scriptargs))
        return rc, out, err

    def _check_run_args(self):
        # Check that this fails due to missing args
        rc, out, err = self._run_bottleneck('bottleneck_test/test_args.py')
        self.assertEqual(rc, 2, atol=0, rtol=0, msg=self._fail_msg('Missing args should error', out + err))

        # This should succeed
        rc, out, err = self._run_bottleneck('bottleneck_test/test_args.py', '--foo foo --bar bar')
        self.assertEqual(rc, 0, atol=0, rtol=0, msg=self._fail_msg('Should pass args to script', out + err))

    def _fail_msg(self, msg, output):
        return '{}, output was:\n{}'.format(msg, output)

    def _check_environment_summary(self, output):
        results = re.search('Environment Summary', output)
        self.assertIsNotNone(results, self._fail_msg('Should have Environment Summary', output))

        # Up to five lines away from the heading, there should be the version number
        results = re.search(r'Environment Summary.*(\n.*){,5}\nPyTorch \d+\.\d+', output)
        self.assertIsNotNone(results, self._fail_msg('Should have PyTorch version', output))

    def _check_cprof_summary(self, output):
        results = re.search('cProfile output', output)
        self.assertIsNotNone(results, self._fail_msg('Should have cProfile output', output))

        # This assumes that after the cProfile output section we have
        # the autograd profiler output
        results = re.search(r'cProfile output.*(\n.*){6,50}\n.*autograd profiler output', output)
        self.assertIsNotNone(results, self._fail_msg(
            'Distance between cProfile and autograd prof out not in [6, 50] lines', output))

    def _check_autograd_summary(self, output):
        results = re.search('autograd profiler output', output)
        self.assertIsNotNone(results, self._fail_msg('Should have autograd profiler output', output))

        # This assumes that after the autograd profiler output is the end of the
        # output.
        results = re.search(r'autograd profiler output.*(\n.*){6,100}', output)
        self.assertIsNotNone(results, self._fail_msg(
            'Distance between autograd prof output and end of output not in [6, 100] lines', output))

    def _check_cuda(self, output):
        if HAS_CUDA:
            results = re.search('CUDA mode', output)
            self.assertIsNotNone(results, self._fail_msg('Should tell users CUDA', output))
        else:
            results = re.search('CUDA mode', output)
            self.assertIsNone(results, self._fail_msg('Should not tell users about CUDA', output))

    @unittest.skipIf(HAS_CUDA, 'CPU-only test')
    def test_bottleneck_cpu_only(self):
        rc, out, err = self._run_bottleneck('bottleneck_test/test.py')
        self.assertEqual(rc, 0, msg='Run failed with\n{}'.format(err))

        self._check_run_args()
        self._check_environment_summary(out)
        self._check_autograd_summary(out)
        self._check_cprof_summary(out)
        self._check_cuda(out)

    @unittest.skipIf(not HAS_CUDA, 'No CUDA')
    def test_bottleneck_cuda(self):
        rc, out, err = self._run_bottleneck('bottleneck_test/test_cuda.py')
        self.assertEqual(rc, 0, msg='Run failed with\n{}'.format(err))

        self._check_run_args()
        self._check_environment_summary(out)
        self._check_autograd_summary(out)
        self._check_cprof_summary(out)
        self._check_cuda(out)


from torch.utils.collect_env import get_pretty_env_info


class TestCollectEnv(TestCase):
    def test_smoke(self):
        info_output = get_pretty_env_info()
        self.assertTrue(info_output.count('\n') >= 17)


class TestONNXUtils(TestCase):
    def test_prepare_onnx_paddings(self):
        sizes = [2, 3, 4]
        pad = [1, 2, 3, 4]
        paddings = _prepare_onnx_paddings(len(sizes), pad)
        self.assertEqual(paddings, [0, 3, 1, 0, 4, 2])

    def test_check_onnx_broadcast(self):

        def try_check_onnx_broadcast(dims1, dims2, expect_broadcast, expect_fail):
            broadcast = True
            fail = False
            try:
                broadcast = check_onnx_broadcast(dims1, dims2)
            except ValueError:
                fail = True
            self.assertEqual(broadcast, expect_broadcast)
            self.assertEqual(fail, expect_fail)

        # Case 1, check the case when len(dims1) < len(dims2) and numel(dims2) > 1
        dims1 = [3, 4]
        dims2 = [2, 3, 4]
        try_check_onnx_broadcast(dims1, dims2, True, True)

        # Case 2, check the case when len(dims1) < len(dims2) and numel(dims2) == 1
        dims1 = [3, 4]
        dims2 = [1, 1, 1]
        try_check_onnx_broadcast(dims1, dims2, True, False)

        # Case 3, check the case when len(dims1) > len(dims2) and numel(dims2) == 1
        dims1 = [1, 1]
        dims2 = [1]
        try_check_onnx_broadcast(dims1, dims2, True, False)

        # Case 4, check the case when len(dims1) > len(dims2) and dims1[x:] == dims2
        dims1 = [2, 3, 4]
        dims2 = [3, 4]
        try_check_onnx_broadcast(dims1, dims2, True, False)

        # Case 5, check the case when len(dims1) > len(dims2), but dims1[x:] != dims2
        dims1 = [2, 3, 4]
        dims2 = [1, 4]
        try_check_onnx_broadcast(dims1, dims2, True, True)

        # Case 6, check the equal case, no broadcast
        dims1 = [3, 4]
        dims2 = [3, 4]
        try_check_onnx_broadcast(dims1, dims2, False, False)

        # Case 7, check the case when len(dims1) == len(dims2), but dims1 != dims2
        dims1 = [3, 4]
        dims2 = [1, 4]
        try_check_onnx_broadcast(dims1, dims2, True, True)

        # Case 8, check the case when len(dims1) == len(dims2) and numel(s2) == 1
        dims1 = [3, 4]
        dims2 = [1, 1]
        try_check_onnx_broadcast(dims1, dims2, True, False)


def sum_of_state_dict(state_dict):
    s = 0
    for _, v in state_dict.items():
        s += v.sum()
    return s

SUM_OF_HUB_EXAMPLE = 431080
TORCHHUB_EXAMPLE_RELEASE_URL = 'https://github.com/ailzhang/torchhub_example/releases/download/0.1/mnist_init_ones'

@unittest.skipIf(IS_SANDCASTLE, 'Sandcastle cannot ping external')
class TestHub(TestCase):
    @retry(URLError, tries=3, skip_after_retries=True)
    def test_load_from_github(self):
        hub_model = hub.load(
            'ailzhang/torchhub_example',
            'mnist',
            source='github',
            pretrained=True,
            verbose=False)
        self.assertEqual(sum_of_state_dict(hub_model.state_dict()),
                         SUM_OF_HUB_EXAMPLE)

    @retry(URLError, tries=3, skip_after_retries=True)
    def test_load_from_local_dir(self):
        local_dir = hub._get_cache_or_reload(
            'ailzhang/torchhub_example', force_reload=False)
        hub_model = hub.load(
            local_dir,
            'mnist',
            source='local',
            pretrained=True,
            verbose=False)
        self.assertEqual(sum_of_state_dict(hub_model.state_dict()),
                         SUM_OF_HUB_EXAMPLE)

    @retry(URLError, tries=3, skip_after_retries=True)
    def test_load_from_branch(self):
        hub_model = hub.load(
            'ailzhang/torchhub_example:ci/test_slash',
            'mnist',
            pretrained=True,
            verbose=False)
        self.assertEqual(sum_of_state_dict(hub_model.state_dict()),
                         SUM_OF_HUB_EXAMPLE)

    @retry(URLError, tries=3, skip_after_retries=True)
    def test_set_dir(self):
        temp_dir = tempfile.gettempdir()
        hub.set_dir(temp_dir)
        hub_model = hub.load(
            'ailzhang/torchhub_example',
            'mnist',
            pretrained=True,
            verbose=False)
        self.assertEqual(sum_of_state_dict(hub_model.state_dict()),
                         SUM_OF_HUB_EXAMPLE)
        assert os.path.exists(temp_dir + '/ailzhang_torchhub_example_master')
        shutil.rmtree(temp_dir + '/ailzhang_torchhub_example_master')

    @retry(URLError, tries=3, skip_after_retries=True)
    def test_list_entrypoints(self):
        entry_lists = hub.list('ailzhang/torchhub_example', force_reload=True)
        self.assertObjectIn('mnist', entry_lists)

    @retry(URLError, tries=3, skip_after_retries=True)
    def test_download_url_to_file(self):
        temp_file = os.path.join(tempfile.gettempdir(), 'temp')
        hub.download_url_to_file(TORCHHUB_EXAMPLE_RELEASE_URL, temp_file, progress=False)
        loaded_state = torch.load(temp_file)
        self.assertEqual(sum_of_state_dict(loaded_state),
                         SUM_OF_HUB_EXAMPLE)

    @retry(URLError, tries=3, skip_after_retries=True)
    def test_load_state_dict_from_url(self):
        loaded_state = hub.load_state_dict_from_url(TORCHHUB_EXAMPLE_RELEASE_URL)
        self.assertEqual(sum_of_state_dict(loaded_state),
                         SUM_OF_HUB_EXAMPLE)

    @retry(URLError, tries=3, skip_after_retries=True)
    def test_load_zip_checkpoint(self):
        hub_model = hub.load(
            'ailzhang/torchhub_example',
            'mnist_zip',
            pretrained=True,
            verbose=False)
        self.assertEqual(sum_of_state_dict(hub_model.state_dict()),
                         SUM_OF_HUB_EXAMPLE)

    # Test the default zipfile serialization format produced by >=1.6 release.
    @retry(URLError, tries=3, skip_after_retries=True)
    def test_load_zip_1_6_checkpoint(self):
        hub_model = hub.load(
            'ailzhang/torchhub_example',
            'mnist_zip_1_6',
            pretrained=True,
            verbose=False)
        self.assertEqual(sum_of_state_dict(hub_model.state_dict()),
                         SUM_OF_HUB_EXAMPLE)


    def test_hub_dir(self):
        with tempfile.TemporaryDirectory('hub_dir') as dirname:
            torch.hub.set_dir(dirname)
            self.assertEqual(torch.hub.get_dir(), dirname)

    @retry(URLError, tries=3, skip_after_retries=True)
    def test_load_state_dict_from_url_with_name(self):
        with tempfile.TemporaryDirectory('hub_dir') as dirname:
            torch.hub.set_dir(dirname)
            file_name = 'test_file'
            loaded_state = hub.load_state_dict_from_url(TORCHHUB_EXAMPLE_RELEASE_URL, file_name=file_name)
            self.assertTrue(os.path.exists(os.path.join(dirname, 'checkpoints', file_name)))
            self.assertEqual(sum_of_state_dict(loaded_state),
                             SUM_OF_HUB_EXAMPLE)

class TestHipify(TestCase):
    def test_import_hipify(self):
        from torch.utils.hipify import hipify_python # noqa


class TestBenchmarkUtils(TestCase):
    def test_timer(self):
        timer = benchmark_utils.Timer(
            stmt="torch.ones(())",
        )
        sample = timer.timeit(5).median
        self.assertIsInstance(sample, float)

        median = timer.blocked_autorange(min_run_time=0.01).median
        self.assertIsInstance(median, float)

        # We set a very high threshold to avoid flakiness in CI.
        # The internal algorithm is tested in `test_adaptive_timer`
        median = timer.adaptive_autorange(threshold=0.5).median

    class _MockTimer:
        _seed = 0

        _timer_noise_level = 0.05
        _timer_cost = 100e-9  # 100 ns

        _function_noise_level = 0.05
        _function_costs = (
            ("pass", 8e-9),
            ("cheap_fn()", 4e-6),
            ("expensive_fn()", 20e-6),
        )

        def __init__(self, stmt, setup, timer, globals):
            self._random_state = np.random.RandomState(seed=self._seed)
            self._mean_cost = {k: v for k, v in self._function_costs}[stmt]

        def sample(self, mean, noise_level):
            return max(self._random_state.normal(mean, mean * noise_level), 5e-9)

        def timeit(self, number):
            return sum([
                # First timer invocation
                self.sample(self._timer_cost, self._timer_noise_level),

                # Stmt body
                self.sample(self._mean_cost * number, self._function_noise_level),

                # Second timer invocation
                self.sample(self._timer_cost, self._timer_noise_level),
            ])

    def test_adaptive_timer(self):
        class MockTimer(benchmark_utils.Timer):
            _timer_cls = self._MockTimer

        def assert_reprs_match(measurement, expected):
            measurement_repr = re.sub(
                "object at 0x[0-9a-fA-F]+>",
                "object at 0xXXXXXXXXXXXX>",
                repr(measurement)
            )
            self.assertEqual(measurement_repr, textwrap.dedent(expected).strip())

        assert_reprs_match(
            MockTimer("pass").blocked_autorange(min_run_time=10),
            """
            <torch.utils.benchmark.utils.common.Measurement object at 0xXXXXXXXXXXXX>
            pass
              Median: 7.98 ns
              IQR:    0.52 ns (7.74 to 8.26)
              125 measurements, 10000000 runs per measurement, 1 thread"""
        )

        assert_reprs_match(
            MockTimer("pass").adaptive_autorange(),
            """
            <torch.utils.benchmark.utils.common.Measurement object at 0xXXXXXXXXXXXX>
            pass
              Median: 7.86 ns
              IQR:    0.71 ns (7.63 to 8.34)
              6 measurements, 1000000 runs per measurement, 1 thread"""
        )

        assert_reprs_match(
            MockTimer("cheap_fn()").blocked_autorange(min_run_time=10),
            """
            <torch.utils.benchmark.utils.common.Measurement object at 0xXXXXXXXXXXXX>
            cheap_fn()
              Median: 3.98 us
              IQR:    0.27 us (3.85 to 4.12)
              252 measurements, 10000 runs per measurement, 1 thread"""
        )

        assert_reprs_match(
            MockTimer("cheap_fn()").adaptive_autorange(),
            """
            <torch.utils.benchmark.utils.common.Measurement object at 0xXXXXXXXXXXXX>
            cheap_fn()
              Median: 4.16 us
              IQR:    0.22 us (4.04 to 4.26)
              4 measurements, 1000 runs per measurement, 1 thread"""
        )

        assert_reprs_match(
            MockTimer("expensive_fn()").blocked_autorange(min_run_time=10),
            """
            <torch.utils.benchmark.utils.common.Measurement object at 0xXXXXXXXXXXXX>
            expensive_fn()
              Median: 19.97 us
              IQR:    1.35 us (19.31 to 20.65)
              501 measurements, 1000 runs per measurement, 1 thread"""
        )

        assert_reprs_match(
            MockTimer("expensive_fn()").adaptive_autorange(),
            """
            <torch.utils.benchmark.utils.common.Measurement object at 0xXXXXXXXXXXXX>
            expensive_fn()
              Median: 20.79 us
              IQR:    1.09 us (20.20 to 21.29)
              4 measurements, 1000 runs per measurement, 1 thread"""
        )

        class _MockCudaTimer(self._MockTimer):
            # torch.cuda.synchronize is much more expensive than
            # just timeit.default_timer
            _timer_cost = 10e-6

            _function_costs = (
                self._MockTimer._function_costs[0],
                self._MockTimer._function_costs[1],

                # GPU should be faster once there is enough work.
                ("expensive_fn()", 5e-6),
            )

        class MockCudaTimer(benchmark_utils.Timer):
            _timer_cls = _MockCudaTimer

        configurations = (
            (7.9903966e-09, 376, 1000000, MockTimer("pass")),
            (7.8554826e-09, 4, 100000000, MockCudaTimer("pass")),
            (3.9930536e-06, 752, 1000, MockTimer("cheap_fn()")),
            (3.9441239e-06, 8, 100000, MockCudaTimer("cheap_fn()")),
            (1.9994249e-05, 150, 1000, MockTimer("expensive_fn()")),
            (4.9301076e-06, 6, 100000, MockCudaTimer("expensive_fn()")),
        )

        for median, repeats, number_per_run, timer_instance in configurations:
            measurement = timer_instance.blocked_autorange(min_run_time=3)
            self.assertEqual(measurement.median, median)
            self.assertEqual(len(measurement.times), repeats)
            self.assertEqual(measurement.number_per_run, number_per_run)

    @slowTest
    @unittest.skipIf(IS_WINDOWS, "Valgrind is not supported on Windows.")
    def test_collect_callgrind(self):
        timer = benchmark_utils.Timer("y = torch.ones((1,)) + 1")

        # Don't collect baseline to speed up unit test by ~30 seconds.
        stats = timer.collect_callgrind(number=1000, collect_baseline=False)

        self.assertIsInstance(stats.counts(include_lookdict_unicode=False), int)

    def test_compare(self):
        # Simulate several approaches.
        costs = (
            # overhead_optimized_fn()
            (1e-6, 1e-9),

            # compute_optimized_fn()
            (3e-6, 5e-10),

            # special_case_fn()  [square inputs only]
            (1e-6, 4e-10),
        )

        sizes = (
            (16, 16),
            (16, 128),
            (128, 128),
            (4096, 1024),
            (2048, 2048),
        )

        # overhead_optimized_fn()
        class _MockTimer_0(self._MockTimer):
            _function_costs = tuple(
                (f"fn({i}, {j})", costs[0][0] + costs[0][1] * i * j)
                for i, j in sizes
            )

        class MockTimer_0(benchmark_utils.Timer):
            _timer_cls = _MockTimer_0

        # compute_optimized_fn()
        class _MockTimer_1(self._MockTimer):
            _function_costs = tuple(
                (f"fn({i}, {j})", costs[1][0] + costs[1][1] * i * j)
                for i, j in sizes
            )

        class MockTimer_1(benchmark_utils.Timer):
            _timer_cls = _MockTimer_1

        # special_case_fn()
        class _MockTimer_2(self._MockTimer):
            _function_costs = tuple(
                (f"fn({i}, {j})", costs[2][0] + costs[2][1] * i * j)
                for i, j in sizes if i == j
            )

        class MockTimer_2(benchmark_utils.Timer):
            _timer_cls = _MockTimer_2

        results = []
        for i, j in sizes:
            results.append(
                MockTimer_0(
                    f"fn({i}, {j})",
                    label="fn",
                    description=f"({i}, {j})",
                    sub_label="overhead_optimized",
                ).blocked_autorange(min_run_time=10)
            )

            results.append(
                MockTimer_1(
                    f"fn({i}, {j})",
                    label="fn",
                    description=f"({i}, {j})",
                    sub_label="compute_optimized",
                ).blocked_autorange(min_run_time=10)
            )

            if i == j:
                results.append(
                    MockTimer_2(
                        f"fn({i}, {j})",
                        label="fn",
                        description=f"({i}, {j})",
                        sub_label="special_case (square)",
                    ).blocked_autorange(min_run_time=10)
                )

        def check_output(output: str, expected: str):
            # VSCode will strip trailing newlines from `expected`, so we have to match
            # this behavior when comparing output.
            output_str = "\n".join(
                i.rstrip() for i in output.strip().splitlines(keepends=False))

            self.assertEqual(output_str, textwrap.dedent(expected).strip())

        compare = benchmark_utils.Compare(results)

        check_output(
            str(compare),
            """
            [------------------------------------------------- fn ------------------------------------------------]
                                         |  (16, 16)  |  (16, 128)  |  (128, 128)  |  (4096, 1024)  |  (2048, 2048)
            1 threads: --------------------------------------------------------------------------------------------
                  overhead_optimized     |    1.3     |     3.0     |     17.4     |     4174.4     |     4174.4
                  compute_optimized      |    3.1     |     4.0     |     11.2     |     2099.3     |     2099.3
                  special_case (square)  |    1.1     |             |      7.5     |                |     1674.7

            Times are in microseconds (us)."""
        )

        compare.trim_significant_figures()
        check_output(
            str(compare),
            """
            [------------------------------------------------- fn ------------------------------------------------]
                                         |  (16, 16)  |  (16, 128)  |  (128, 128)  |  (4096, 1024)  |  (2048, 2048)
            1 threads: --------------------------------------------------------------------------------------------
                  overhead_optimized     |     1      |     3.0     |      17      |      4200      |      4200
                  compute_optimized      |     3      |     4.0     |      11      |      2100      |      2100
                  special_case (square)  |     1      |             |       8      |                |      1700

            Times are in microseconds (us)."""
        )

        compare.colorize()
        check_output(
            str(compare),
            """
            [------------------------------------------------- fn ------------------------------------------------]
                                         |  (16, 16)  |  (16, 128)  |  (128, 128)  |  (4096, 1024)  |  (2048, 2048)
            1 threads: --------------------------------------------------------------------------------------------
                  overhead_optimized     |     1      |  \x1b[92m\x1b[1m   3.0   \x1b[0m\x1b[0m  |  \x1b[2m\x1b[91m    17    \x1b[0m\x1b[0m  |      4200      |  \x1b[2m\x1b[91m    4200    \x1b[0m\x1b[0m
                  compute_optimized      |  \x1b[2m\x1b[91m   3    \x1b[0m\x1b[0m  |     4.0     |      11      |  \x1b[92m\x1b[1m    2100    \x1b[0m\x1b[0m  |      2100
                  special_case (square)  |  \x1b[92m\x1b[1m   1    \x1b[0m\x1b[0m  |             |  \x1b[92m\x1b[1m     8    \x1b[0m\x1b[0m  |                |  \x1b[92m\x1b[1m    1700    \x1b[0m\x1b[0m

            Times are in microseconds (us)."""  # noqa
        )


    @unittest.skipIf(IS_WINDOWS and os.getenv("VC_YEAR") == "2019", "Random seed only accepts int32")
    def test_fuzzer(self):
        fuzzer = benchmark_utils.Fuzzer(
            parameters=[
                benchmark_utils.FuzzedParameter(
                    "n", minval=1, maxval=16, distribution="loguniform")],
            tensors=[benchmark_utils.FuzzedTensor("x", size=("n",))],
            seed=0,
        )

        expected_results = [
            (0.7821, 0.0536, 0.9888, 0.1949, 0.5242, 0.1987, 0.5094),
            (0.7166, 0.5961, 0.8303, 0.005),
        ]

        for i, (tensors, _, _) in enumerate(fuzzer.take(2)):
            x = tensors["x"]
            self.assertEqual(
                x, torch.Tensor(expected_results[i]), rtol=1e-3, atol=1e-3)


class TestAssert(TestCase):
    def test_assert_true(self):
        # verify assertions work as expected
        torch.Assert(True, "foo")
        with self.assertRaisesRegex(AssertionError, "bar"):
            torch.Assert(False, "bar")


if __name__ == '__main__':
    run_tests()
