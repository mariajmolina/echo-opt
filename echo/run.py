import os
import sys
import yaml
import time
import optuna
import logging
import importlib.machinery

from echo.src.config import (
    config_check,
    configure_storage,
    configure_sampler,
    configure_pruner,
)
from echo.src.reporting import successful_trials, get_sec, devices
import warnings

warnings.filterwarnings("ignore")


# References
# https://github.com/optuna/optuna/issues/1365
# https://docs.dask.org/en/latest/setup/hpc.html
# https://dask-cuda.readthedocs.io/en/latest/worker.html
# https://optuna.readthedocs.io/en/stable/tutorial/004_distributed.html#distributed


start_the_clock = time.time()


def main():

    if len(sys.argv) < 3 or len(sys.argv) > 4:
        print(
            "Usage: python run.py hyperparameter.yml model.yml [optinal PBS/SLURM job ID]"
        )
        sys.exit()

    hyper_fn = str(sys.argv[1])
    model_fn = str(sys.argv[2])

    if len(sys.argv) == 4:
        node_id = str(sys.argv[3])
    else:
        node_id = None

    """ Set up a logger """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(levelname)s:%(name)s:%(message)s")

    """ Stream output to stdout """
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)
    root.addHandler(ch)

    """ Run some tests on the configurations """
    config_check(hyper_fn, model_fn, file_check=True)

    """ Load config files """
    with open(hyper_fn) as f:
        hyper_config = yaml.load(f, Loader=yaml.FullLoader)
    with open(model_fn) as f:
        model_config = yaml.load(f, Loader=yaml.FullLoader)

    """ Get the path to save all the data """
    save_path = hyper_config["save_path"]
    logging.info(f"Saving trial details to {save_path}")

    """ Create the save directory if it does not already exist """
    if not os.path.isdir(save_path):
        logging.info(f"Creating parent save_path at {save_path}")
        os.makedirs(save_path, exist_ok=True)

    """ Stream output to file """
    _log = False if "log" not in hyper_config else hyper_config["log"]
    if _log:
        fh = logging.FileHandler(
            os.path.join(save_path, "log.txt"),
            mode="a+",  # always initiate / append
            encoding="utf-8",
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(formatter)
        root.addHandler(fh)

    """ Print job id to the logger """
    if node_id is not None:
        logging.info(f"Running on PBS/SLURM batch id: {node_id}")
    else:
        logging.info("Running as __main__")

    """ Copy the optuna details to the model config """
    model_config["optuna"] = hyper_config["optuna"]
    model_config["optuna"]["save_path"] = hyper_config["save_path"]

    """ Import user-supplied Objective class """
    logging.info(
        f"Importing custom objective from {model_config['optuna']['objective']}"
    )
    loader = importlib.machinery.SourceFileLoader(
        "custom_objective", model_config["optuna"]["objective"]
    )
    mod = loader.load_module()
    from custom_objective import Objective

    """ Obtain GPU/CPU ids """
    device = devices(model_config["optuna"]["gpu"])

    """ Initialize the study object """
    study_name = model_config["optuna"]["study_name"]

    """ Set up storage db """
    storage = configure_storage(hyper_config)

    """ Initialize the sampler """
    sampler = configure_sampler(hyper_config)

    """  Initialize the pruner """
    pruner = configure_pruner(hyper_config)

    """ Initialize study direction(s) """
    direction = model_config["optuna"]["direction"]
    single_objective = isinstance(direction, str)
    logging.info(f"Direction of optimization: {direction}")

    """ Initialize the optimization metric(s) """
    if isinstance(model_config["optuna"]["metric"], list):
        metric = [str(m) for m in model_config["optuna"]["metric"]]
    else:
        metric = str(model_config["optuna"]["metric"])
    logging.info(f"Using metric {metric}")

    """ Load or initiate study """
    if single_objective:
        study = optuna.create_study(
            study_name=study_name,
            storage=storage,
            sampler=sampler,
            pruner=pruner,
            direction=direction,
            load_if_exists=True,
        )
    else:
        study = optuna.multi_objective.study.create_study(
            study_name=study_name,
            storage=storage,
            sampler=sampler,
            pruner=pruner,
            directions=direction,
            load_if_exists=True,
        )
    logging.info(f"Loaded study {study_name} located at {storage}")

    """ Initialize objective function """
    objective = Objective(model_config, metric)
    objective.set_properties(node_id=node_id, device=device)

    """ Optimize it """
    logging.info(
        f'Running optimization for {model_config["optuna"]["n_trials"]} trials'
    )

    """ Get the cluster job wall-time """
    if "slurm" in hyper_config:
        wall_time = hyper_config["slurm"]["batch"]["t"]
    elif "pbs" in hyper_config:
        wall_time = False
        for option in hyper_config["pbs"]["batch"]["l"]:
            if "walltime" in option:
                wall_time = option.split("walltime=")[-1]
                break
        if wall_time is False:
            logging.warning(
                "Could not process the walltime for run.py. Assuming 12 hours."
            )
            wall_time = "12:00:00"
    wall_time_secs = get_sec(wall_time)

    logging.warning("Attempting to run trials and stop before hitting the wall-time")
    logging.warning(
        "Some trials may not complete if the wall-time is reached. Optuna will start over."
    )

    estimated_run_time = wall_time_secs - (time.time() - start_the_clock)
    while successful_trials(study) < model_config["optuna"]["n_trials"]:
        try:
            study.optimize(
                objective,
                n_trials=1,
                timeout=estimated_run_time,
                # catch = (ValueError,)
            )
        except KeyboardInterrupt:
            logging.warning("Recieved signal to die from keyboard. Exiting.")
            break
        except Exception as E:
            logging.warning(f"Died due to due to error {E}")
            break
            # continue

        """ Early stopping if too close to the wall time """
        df = study.trials_dataframe()
        if df.shape[0] > 1:
            df["run_time"] = df["datetime_complete"] - df["datetime_start"]
            completed_runs = df["datetime_complete"].apply(
                lambda x: True if x else False
            )
            run_times = df["run_time"][completed_runs].apply(
                lambda x: x.total_seconds() / 3600.0
            )
            average_run_time = run_times.mean()
            sigma_run_time = run_times.std()

            estimated_run_time = average_run_time + 2 * sigma_run_time
            time_left = wall_time_secs - (time.time() - start_the_clock)
            if time_left < estimated_run_time:
                logging.warning(
                    "Stopping early as estimated run-time exceeds the time remaining on this node."
                )
                break

        else:  # no trials in the database yet
            time_left = wall_time_secs - (time.time() - start_the_clock)
            if time_left < (
                wall_time_secs / 2
            ):  # if more than half the time remaining, launch another trial
                logging.warning(
                    "Stopping early as estimated run-time exceeds the time remaining on this node."
                )
                break
            else:
                estimated_run_time = 0.95 * time_left


if __name__ == "__main__":
    main()
