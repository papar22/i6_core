__all__ = ["ReturnnModel", "ReturnnTrainingJob"]

from sisyphus import *

Path = setup_path(__package__)

import copy
import os
import shutil
import stat
import subprocess as sp

import recipe.i6_asr.util as util

from .config import ReturnnConfig


class ReturnnModel:
    def __init__(self, returnn_config_file, model, epoch):
        self.returnn_config_file = returnn_config_file
        self.model = model
        self.epoch = epoch


class Checkpoint:
    def __init__(self, ckpt_path, index_path):
        self.ckpt_path = ckpt_path
        self.index_path = index_path

    def _sis_hash(self):
        return self.index_path._sis_hash()

    def __str__(self):
        return self.ckpt_path

    def __repr__(self):
        return "'%s'" % self.ckpt_path


class ReturnnTrainingJob(Job):
    def __init__(
        self,
        train_data,
        dev_data,
        returnn_config,
        num_classes=None,
        *,  # args below are keyword only
        log_verbosity=3,
        device="gpu",
        num_epochs=1,
        save_interval=1,
        keep_epochs=None,
        time_rqmt=4,
        mem_rqmt=4,
        cpu_rqmt=2,
        horovod_num_processes=None,
        returnn_python_exe=None,
        returnn_root=None
    ):
        assert isinstance(returnn_config, ReturnnConfig)
        kwargs = locals()
        del kwargs["self"]

        self.returnn_python_exe = (
            returnn_python_exe
            if returnn_python_exe is not None
            else gs.RETURNN_PYTHON_EXE
        )
        self.returnn_root = (
            returnn_root if returnn_root is not None else gs.RETURNN_ROOT
        )
        self.num_classes = num_classes

        self.returnn_config = ReturnnTrainingJob.create_returnn_config(**kwargs)

        stored_epochs = list(range(save_interval, num_epochs, save_interval)) + [
            num_epochs
        ]
        if keep_epochs is None:
            self.keep_epochs = set(stored_epochs)
        else:
            self.keep_epochs = set(keep_epochs)

        suffix = ".meta" if self.returnn_config.get("use_tensorflow", False) else ""

        self.returnn_config_file = self.output_path("returnn.config")
        self.learning_rates = self.output_path("learning_rates")
        self.model_dir = self.output_path("models", directory=True)
        self.models = {
            k: ReturnnModel(
                self.returnn_config_file,
                self.output_path("models/epoch.%.3d%s" % (k, suffix)),
                k,
            )
            for k in stored_epochs
            if k in self.keep_epochs
        }
        if self.returnn_config.get("use_tensorflow", False):
            self.checkpoints = {
                k: Checkpoint(index_path.get_path()[: -len(".index")], index_path)
                for k in stored_epochs
                if k in self.keep_epochs
                for index_path in [self.output_path("models/epoch.%.3d.index" % k)]
            }
        self.plot_se = self.output_path("score_and_error.png")
        self.plot_lr = self.output_path("learning_rate.png")

        self.returnn_config.post_config["model"] = os.path.join(
            self.model_dir.get_path(), "epoch"
        )

        self.use_horovod = True if (horovod_num_processes is not None) else False
        self.horovod_num_processes = horovod_num_processes

        self.rqmt = {
            "gpu": 1 if device == "gpu" else 0,
            "cpu": cpu_rqmt,
            "mem": mem_rqmt,
            "time": time_rqmt,
        }

        if self.use_horovod:
            self.rqmt["cpu"] *= self.horovod_num_processes
            self.rqmt["gpu"] *= self.horovod_num_processes
            self.rqmt["mem"] *= self.horovod_num_processes

    def _get_run_cmd(self):
        run_cmd = [
            tk.uncached_path(self.returnn_python_exe),
            os.path.join(tk.uncached_path(self.returnn_root), "rnn.py"),
            self.returnn_config_file.get_path(),
        ]

        if self.use_horovod:
            run_cmd = [
                "mpirun",
                "-np",
                str(self.horovod_num_processes),
                "-bind-to",
                "none",
                "-map-by",
                "slot",
                "-mca",
                "pml",
                "ob1",
                "-mca",
                "btl",
                "^openib",
                "--report-bindings",
            ] + run_cmd

        return run_cmd

    def path_available(self, path):
        # if job is finised the path is available
        res = super().path_available(path)
        if res:
            return res

        # learning rate files are only available at the end
        if path == self.learning_rates:
            return super().path_available(path)

        # maybe the file already exists
        res = os.path.exists(path.get_path())
        if res:
            return res

        # maybe the model is just a pretrain model
        file = os.path.basename(path.get_path())
        directory = os.path.dirname(path.get_path())
        if file.startswith("epoch."):
            segments = file.split(".")
            pretrain_file = ".".join([segments[0], "pretrain", segments[1]])
            pretrain_path = os.path.join(directory, pretrain_file)
            return os.path.exists(pretrain_path)

        return False

    def tasks(self):
        yield Task("create_files", mini_task=True)
        yield Task("run", resume="run", rqmt=self.rqmt)
        yield Task("plot", resume="plot", mini_task=True)

    def create_files(self):
        # returnn
        config = self.returnn_config
        if self.num_classes is not None:
            if "num_outputs" not in config.config:
                config.config["num_outputs"] = {}
            config.config["num_outputs"]["classes"] = [
                util.get_val(self.num_classes),
                1,
            ]
        config.write(self.returnn_config_file.get_path())

        with open("rnn.sh", "wt") as f:
            f.write("#!/usr/bin/env bash\n%s" % " ".join(self._get_run_cmd()))
        os.chmod(
            "rnn.sh",
            stat.S_IRUSR
            | stat.S_IRGRP
            | stat.S_IROTH
            | stat.S_IWUSR
            | stat.S_IXUSR
            | stat.S_IXGRP
            | stat.S_IXOTH,
        )

    @staticmethod
    def _relink(src, dst):
        if os.path.exists(dst):
            os.remove(dst)
        os.link(src, dst)

    def run(self):
        sp.check_call(self._get_run_cmd())

        lrf = self.returnn_config.get("learning_rate_file", "learning_rates")
        self._relink(lrf, self.learning_rates.get_path())

        # cleanup
        if hasattr(self, "keep_epochs"):
            for e in os.scandir(self.model_dir.get_path()):
                if e.is_file() and e.name.startswith("epoch."):
                    s = e.name.split(".")
                    idx = 2 if s[1] == "pretrain" else 1
                    epoch = int(s[idx])
                    if epoch not in self.keep_epochs:
                        os.unlink(e.path)

    def plot(self):
        def EpochData(learningRate, error):
            return {"learning_rate": learningRate, "error": error}

        with open(self.learning_rates.get_path(), "rt") as f:
            text = f.read()

        data = eval(text)

        epochs = list(sorted(data.keys()))
        train_score_keys = [
            k for k in data[epochs[0]]["error"] if k.startswith("train_score")
        ]
        dev_score_keys = [
            k for k in data[epochs[0]]["error"] if k.startswith("dev_score")
        ]
        dev_error_keys = [
            k for k in data[epochs[0]]["error"] if k.startswith("dev_error")
        ]

        train_scores = [
            [
                (epoch, data[epoch]["error"][tsk])
                for epoch in epochs
                if tsk in data[epoch]["error"]
            ]
            for tsk in train_score_keys
        ]
        dev_scores = [
            [
                (epoch, data[epoch]["error"][dsk])
                for epoch in epochs
                if dsk in data[epoch]["error"]
            ]
            for dsk in dev_score_keys
        ]
        dev_errors = [
            [
                (epoch, data[epoch]["error"][dek])
                for epoch in epochs
                if dek in data[epoch]["error"]
            ]
            for dek in dev_error_keys
        ]
        learing_rates = [data[epoch]["learning_rate"] for epoch in epochs]

        colors = ["#2A4D6E", "#AA3C39", "#93A537"]  # blue red yellowgreen

        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax1 = plt.subplots()
        for ts in train_scores:
            ax1.plot([d[0] for d in ts], [d[1] for d in ts], "o-", color=colors[0])
        for ds in dev_scores:
            ax1.plot([d[0] for d in ds], [d[1] for d in ds], "o-", color=colors[1])
        ax1.set_xlabel("epoch")
        ax1.set_ylabel("scores", color=colors[0])
        for tl in ax1.get_yticklabels():
            tl.set_color(colors[0])

        if len(dev_errors) > 0 and any(len(de) > 0 for de in dev_errors):
            ax2 = ax1.twinx()
            ax2.set_ylabel("dev error", color=colors[2])
            for de in dev_errors:
                ax2.plot([d[0] for d in de], [d[1] for d in de], "o-", color=colors[2])
            for tl in ax2.get_yticklabels():
                tl.set_color(colors[2])

        fig.savefig(fname=self.plot_se.get_path())

        fig, ax1 = plt.subplots()
        ax1.semilogy(epochs, learing_rates, "ro-")
        ax1.set_xlabel("epoch")
        ax1.set_ylabel("learning_rate")

        fig.savefig(fname=self.plot_lr.get_path())

    @classmethod
    def create_returnn_config(
        cls,
        train_data,
        dev_data,
        returnn_config,
        log_verbosity,
        device,
        num_epochs,
        save_interval,
        horovod_num_processes,
        **kwargs
    ):
        assert device in ["gpu", "cpu"]
        assert "network" in returnn_config.config

        res = copy.deepcopy(returnn_config)

        config = {
            "task": "train",
            "target": "classes",
            "learning_rate_file": "learning_rates",
        }

        post_config = {
            "device": device,
            "log": ["./returnn.log"],
            "log_verbosity": log_verbosity,
            "num_epochs": num_epochs,
            "save_interval": save_interval,
            "multiprocessing": True,
        }

        if horovod_num_processes is not None:
            config["use_horovod"] = True

        config.update(copy.deepcopy(returnn_config.config))
        if returnn_config.post_config is not None:
            post_config.update(copy.deepcopy(returnn_config.post_config))

        # update train and dev data settings with rasr dataset
        if "train" in config:
            config["train"] = {**config["train"].copy(), **train_data}
        else:
            config["train"] = train_data
        if "dev" in config:
            config["dev"] = {**config["dev"].copy(), **dev_data}
        else:
            config["dev"] = dev_data

        res.config = config
        res.post_config = post_config

        return res

    @classmethod
    def hash(cls, kwargs):
        returnn_config = kwargs["returnn_config"]
        extra_python_hash = (
            returnn_config.extra_python
            if returnn_config.extra_python_hash is None
            else returnn_config.extra_python_hash
        )

        d = {
            "returnn_config": returnn_config.config,
            "extra_python": extra_python_hash,
            "returnn_python_exe": kwargs["returnn_python_exe"],
            "returnn_root": kwargs["returnn_root"],
            "train_data": kwargs["train_data"],
            "dev_data": kwargs["dev_data"],
        }

        if kwargs["horovod_num_processes"] is not None:
            d["horovod_num_processes"] = kwargs["horovod_num_processes"]

        return super().hash(d)


class ReturnnTrainingFromFile(Job):
    """
    The Job allows to directly execute returnn config files. The config files have to have the line
    `ext_model = config.value("ext_model", None)` and `model = ext_model` to correctly set the model path

    If the learning rate file should be available, add
    `ext_learning_rate_file = config.value("ext_learning_rate_file", None)` and
    `learning_rate_file = ext_learning_rate_file`

    Other externally controllable parameters may also defined in the same way, and can be set by providing the parameter
    value in the parameter_dict. The "ext_" prefix is used for naming convention only, but should be used for all
    external parameters to clearly mark them instead of simply overwriting any normal parameter.

    Also make sure that task="train" is set.
    """

    def __init__(
        self,
        returnn_config_file,
        parameter_dict,
        time_rqmt=4,
        mem_rqmt=4,
        returnn_python_exe=None,
        returnn_root=None,
    ):
        """

        :param tk.Path|str returnn_config_file: a returnn training config file
        :param dict parameter_dict: provide external parameters to the rnn.py call
        :param int|str time_rqmt:
        :param int|str mem_rqmt:
        :param tk.Path|str returnn_python_exe: the executable for running returnn
        :param tk.Path |str returnn_root: the path to the returnn source folder
        """

        self.returnn_python_exe = (
            returnn_python_exe
            if returnn_python_exe is not None
            else gs.RETURNN_PYTHON_EXE
        )
        self.returnn_root = (
            returnn_root if returnn_root is not None else gs.RETURNN_ROOT
        )

        self.returnn_config_file_in = returnn_config_file
        self.parameter_dict = parameter_dict
        if self.parameter_dict is None:
            self.parameter_dict = {}

        self.returnn_config_file = self.output_path("returnn.config")

        self.rqmt = {"gpu": 1, "cpu": 2, "mem": mem_rqmt, "time": time_rqmt}

        self.learning_rates = self.output_path("learning_rates")
        self.model_dir = self.output_path("models", directory=True)

        self.parameter_dict["ext_model"] = tk.uncached_path(self.model_dir) + "/epoch"
        self.parameter_dict["ext_learning_rate_file"] = tk.uncached_path(
            self.learning_rates
        )

    def tasks(self):
        yield Task("create_files", mini_task=True)
        yield Task("run", resume="run", rqmt=self.rqmt)

    def path_available(self, path):
        # if job is finised the path is available
        res = super().path_available(path)
        if res:
            return res

        # learning rate files are only available at the end
        if path == self.learning_rates:
            return super().path_available(path)

        # maybe the file already exists
        res = os.path.exists(path.get_path())
        if res:
            return res

        # maybe the model is just a pretrain model
        file = os.path.basename(path.get_path())
        directory = os.path.dirname(path.get_path())
        if file.startswith("epoch."):
            segments = file.split(".")
            pretrain_file = ".".join([segments[0], "pretrain", segments[1]])
            pretrain_path = os.path.join(directory, pretrain_file)
            return os.path.exists(pretrain_path)

        return False

    def get_parameter_list(self):
        parameter_list = []
        for k, v in sorted(self.parameter_dict.items()):
            if isinstance(v, tk.Variable):
                v = v.get()
            elif isinstance(v, tk.Path):
                v = tk.uncached_path(v)
            elif isinstance(v, (list, dict, tuple)):
                v = '"%s"' % str(v).replace(" ", "")

            if isinstance(v, (float, int)) and v < 0:
                v = "+" + str(v)
            else:
                v = str(v)

            parameter_list.append("++%s" % k)
            parameter_list.append(v)

        return parameter_list

    def create_files(self):
        # returnn
        shutil.copy(
            tk.uncached_path(self.returnn_config_file_in),
            tk.uncached_path(self.returnn_config_file),
        )

        parameter_list = self.get_parameter_list()

        with open("rnn.sh", "wt") as f:
            cmd = [
                tk.uncached_path(self.returnn_python_exe),
                os.path.join(tk.uncached_path(self.returnn_root), "rnn.py"),
                self.returnn_config_file.get_path(),
            ]
            f.write("#!/usr/bin/env bash\n%s" % " ".join(cmd + parameter_list))
        os.chmod(
            "rnn.sh",
            stat.S_IRUSR
            | stat.S_IRGRP
            | stat.S_IROTH
            | stat.S_IWUSR
            | stat.S_IXUSR
            | stat.S_IXGRP
            | stat.S_IXOTH,
        )

    def run(self):
        sp.check_call(["./rnn.sh"])

    @classmethod
    def hash(cls, kwargs):

        d = {
            "returnn_config_file": kwargs["returnn_config_file"],
            "parameter_dict": kwargs["parameter_dict"],
            "returnn_python_exe": kwargs["returnn_python_exe"],
            "returnn_root": kwargs["returnn_root"],
        }

        return super().hash(d)