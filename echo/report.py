from echo.src.config import (
    configure_storage,
    configure_pruner,
)
from echo.src.reporting import study_report
from echo.src.config import recursive_update

import os
import yaml
import optuna
import logging
import matplotlib as mpl
import matplotlib.pyplot as plt
from argparse import ArgumentParser
from typing import Dict

import warnings

warnings.filterwarnings("ignore")


def args():
    parser = ArgumentParser(
        description="report.py: Get the status/progress of a hyperparameter study"
    )

    parser.add_argument(
        "hyperparameter",
        type=str,
        help="Path to the hyperparameter configuration containing your inputs.",
    )

    parser.add_argument(
        "-m",
        "--model",
        dest="model",
        type=str,
        default=False,
        help="A yaml structured file containining settings for model/training parameters",
    )

    parser.add_argument(
        "-p",
        "--plot",
        dest="plot",
        type=str,
        default=False,
        help="A yaml structured file containining settings for matplotlib/pylab objects",
    )

    parser.add_argument(
        "-t",
        "--n_trees",
        dest="n_trees",
        type=int,
        default=64,
        help="The number of trees to use in parameter importance models. Default is 64.",
    )

    parser.add_argument(
        "-d",
        "--max_depth",
        dest="max_depth",
        type=int,
        default=64,
        help="The maximum depth to use in parameter importance models. Default is 64.",
    )

    return vars(parser.parse_args())


def update_figure(
    fig: mpl.figure.Figure, params: Dict[str, str] = False
) -> mpl.figure.Figure:
    """
    Updates some mpl Figure parameters. Only limited support for now.
    In a future version the optuna plots will be moved here
    and expanded customization will be enabled.

    Returns a matplotlib Figure

    Inputs:
        fig: a matplotlib Figure
        params: a dictionary containing mpl fields
    """

    if params is False:
        fig.set_yscale("log")
        mpl.rcParams.update({"figure.dpi": 300})
    else:
        if "rcparams" in params:
            mpl.rcParams.update(**params["rcparams"])
        if "set_xlim" in params:
            fig.set_xlim(params["set_xlim"])
        if "set_ylim" in params:
            fig.set_ylim(params["set_ylim"])
        if "set_xscale" in params:
            fig.set_xscale(params["set_xscale"])
        if "set_yscale" in params:
            fig.set_yscale(params["set_yscale"])

    plt.tight_layout()
    return fig


def plot_wrapper(
    study: optuna.study.Study,
    identifier: str,
    save_path: str,
    params: Dict[str, str] = False,
):

    """
    Creates and saves an intermediate values plot.

    Does not return.

    Inputs:
        study: an Optuna study object
        identifier: a string identifier for selecting the optuna plot method
        save_path: a path where the plot should be saved
        params: a dictionary containing mpl fields. Default = False
    """

    flag = isinstance(params, dict)
    if flag and identifier in params:
        params = params[identifier]
    else:
        flag = False

    # Use optunas mpl object for now
    if identifier == "intermediate_values":
        fig = optuna.visualization.matplotlib.plot_intermediate_values(study)
    elif identifier == "optimization_history":
        fig = optuna.visualization.matplotlib.plot_optimization_history(study)
    elif identifier == "pareto_front":
        fig = optuna.multi_objective.visualization.plot_pareto_front(study)
    else:
        raise OSError(f"An incorrect optuna plot identifier {identifier} was used")

    fig = update_figure(fig, params)

    if flag and "save_path" in params:
        save_path = params["save_path"]

    figure_save_path = os.path.join(save_path, f"{identifier}.pdf")
    plt.savefig(figure_save_path)

    logging.info(f"Saving the {identifier} plot to file at {figure_save_path}")


def main():

    args_dict = args()

    hyper_config = args_dict.pop("hyperparameter")
    model_config = args_dict.pop("model") if "model" in args_dict else False
    plot_config = args_dict.pop("plot") if "plot" in args_dict else False

    """ Options for the parameter importance tree models """
    n_trees = args_dict.pop("n_trees")
    max_depth = args_dict.pop("max_depth")

    """ Check if hyperparameter config file exists """
    assert os.path.isfile(
        hyper_config
    ), f"Hyperparameter optimization config file {hyper_config} does not exist"
    with open(hyper_config) as f:
        hyper_config = yaml.load(f, Loader=yaml.FullLoader)

    if model_config is not False:
        assert os.path.isfile(
            model_config
        ), f"Model config file {model_config} does not exist"
        with open(model_config) as f:
            model_config = yaml.load(f, Loader=yaml.FullLoader)

    if plot_config is not False:
        """Check if plot config file exists"""
        assert os.path.isfile(
            plot_config
        ), f"Hyperparameter optimization plot file {plot_config} does not exist"
        with open(plot_config) as p:
            plot_config = yaml.load(p, Loader=yaml.FullLoader)

    """ Set up a logger """
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(levelname)s:%(name)s:%(message)s")

    """ Stream output to stdout """
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)
    root.addHandler(ch)

    """ Get the save path """
    save_path = hyper_config["save_path"]

    """ Initialize the study object """
    study_name = hyper_config["optuna"]["study_name"]

    """ Set up storage db """
    storage = configure_storage(hyper_config)

    """ Initialize the pruner """
    pruner = configure_pruner(hyper_config)

    """ Initialize study direction(s) """
    direction = hyper_config["optuna"]["direction"]
    single_objective = isinstance(direction, str)

    """ Load from database """
    if single_objective:
        study = optuna.load_study(study_name=study_name, storage=storage, pruner=pruner)
    else:
        study = optuna.multi_objective.study.load_study(
            study_name=study_name, storage=storage, pruner=pruner
        )

    """ Print the study report """
    complete_trials = study_report(study, hyper_config)
    logging.info(f"Best trial: {study.best_trial.value}")
    logging.info("Best parameters in the study:")
    for param, val in study.best_params.items():
        logging.info(f"\t{param}: {val}")

    save_fn = os.path.join(save_path, f"{study_name}.csv")
    logging.info(f"Saving the results of the study to file at {save_fn}")
    study.trials_dataframe().to_csv(save_fn, index=None)

    """ Save best parameters to new model configuration """
    if model_config:
        best_fn = os.path.join(save_path, "best.yml")
        logging.info(f"Saving the best model configuration to {best_fn}")
        best_params = study.best_params
        hyperparameters = hyper_config["optuna"]["parameters"]
        for named_parameter, _ in hyperparameters.items():
            if ":" in named_parameter:
                split_name = named_parameter.split(":")
                best_value = best_params[split_name[-1]]
                recursive_update(
                    split_name,
                    model_config,
                    best_value,
                )
            else:
                if named_parameter in model_config:
                    model_config[named_parameter] = best_params[named_parameter]
        with open(best_fn, "w") as fid:
            yaml.dump(model_config, fid, default_flow_style=False)
    else:
        logging.warning(
            "A model configuration is required to save the best hyperparameters"
        )
        logging.warning("\tRun echo-report --help for details")

    """ Create the optuna-supported figures """
    if single_objective:
        """Plot the optimization_history"""
        logging.info(f"Saving the optimization_history.pdf to {save_path}")
        plot_wrapper(study, "optimization_history", save_path, plot_config)

        if not isinstance(pruner, optuna.pruners.NopPruner):
            """Plot the intermediate_values"""
            logging.info(f"Saving the intermediate_values.pdf to {save_path}")
            plot_wrapper(study, "intermediate_values", save_path, plot_config)
    else:
        """Plot the pareto front"""
        logging.info(f"Saving the pareto_front.pdf to {save_path}")
        plot_wrapper(study, "pareto_front", save_path, plot_config)

    """ Compute the optuna-supported parameter importances """
    if complete_trials > 1:
        try:
            logging.info("Computing fAVNOVAimportances, this make take awhile")
            f_importance = optuna.importance.FanovaImportanceEvaluator(
                n_trees=n_trees, max_depth=max_depth
            ).evaluate(study=study)
            favnova = dict(f_importance)
            logging.info("Computing MDI importances, this make take awhile")
            mdi_importance = optuna.importance.MeanDecreaseImpurityImportanceEvaluator(
                n_trees=n_trees, max_depth=max_depth
            ).evaluate(study=study)
            mdi = dict(mdi_importance)
            logging.info("\tParameter\tfANOVA\t\tMDI")
            for key, val in favnova.items():
                mdi_val = mdi[key]
                logging.info(f"\t{key}\t{val:.6f}\t{mdi_val:6f}")
        except Exception as E:  # Encountered zero total variance in all trees.
            logging.warning(f"Failed to compute parameter importance due to error: {E}")
            pass


if __name__ == "__main__":
    main()
