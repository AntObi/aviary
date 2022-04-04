import os

import numpy as np
import torch
from matminer.utils.io import load_dataframe_from_json
from sklearn.metrics import r2_score
from sklearn.model_selection import train_test_split as split

from aviary.cgcnn.data import CrystalGraphData, collate_batch
from aviary.cgcnn.model import CrystalGraphConvNet
from aviary.cgcnn.utils import get_cgcnn_input
from aviary.utils import results_multitask, train_ensemble

torch.manual_seed(0)  # ensure reproducible results


def test_cgcnn_regression():
    data_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data/matbench_phonons.json.gz"
    )
    elem_emb = "cgcnn92"
    targets = ["last phdos peak"]
    tasks = ["regression"]
    losses = ["L1"]
    robust = True
    model_name = "cgcnn-reg-test"
    elem_fea_len = 32
    h_fea_len = 128
    n_graph = 3
    n_hidden = 1
    ensemble = 2
    run_id = 1
    data_seed = 42
    epochs = 25
    log = False
    sample = 1
    test_size = 0.2
    resume = False
    fine_tune = None
    transfer = None
    optim = "AdamW"
    learning_rate = 3e-4
    momentum = 0.9
    weight_decay = 1e-6
    batch_size = 128
    workers = 0
    device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")

    task_dict = dict(zip(targets, tasks))
    loss_dict = dict(zip(targets, losses))

    assert os.path.exists(data_path), f"{data_path} does not exist!"

    df = load_dataframe_from_json(data_path)
    df["lattice"] = [None] * len(df)
    df["sites"] = [None] * len(df)
    df[["lattice", "sites"]] = df.apply(
        lambda x: get_cgcnn_input(x.structure), axis=1, result_type="expand"
    )
    df["material_id"] = [f"mb_phdos_{i}" for i in range(len(df))]
    df["composition"] = df.structure.apply(
        lambda x: x.composition.formula.replace(" ", "")
    )

    dataset = CrystalGraphData(df=df, elem_emb=elem_emb, task_dict=task_dict)
    n_targets = dataset.n_targets
    elem_emb_len = dataset.elem_emb_len
    nbr_fea_len = dataset.nbr_fea_dim

    train_idx = list(range(len(dataset)))

    print(f"using {test_size} of training set as test set")
    train_idx, test_idx = split(train_idx, random_state=data_seed, test_size=test_size)
    test_set = torch.utils.data.Subset(dataset, test_idx)

    print("No validation set used, using test set for evaluation purposes")
    # NOTE that when using this option care must be taken not to
    # peak at the test-set. The only valid model to use is the one
    # obtained after the final epoch where the epoch count is
    # decided in advance of the experiment.
    val_set = test_set

    train_set = torch.utils.data.Subset(dataset, train_idx[0::sample])

    data_params = {
        "batch_size": batch_size,
        "num_workers": workers,
        "pin_memory": False,
        "shuffle": True,
        "collate_fn": collate_batch,
    }

    setup_params = {
        "optim": optim,
        "learning_rate": learning_rate,
        "weight_decay": weight_decay,
        "momentum": momentum,
        "device": device,
    }

    restart_params = {
        "resume": resume,
        "fine_tune": fine_tune,
        "transfer": transfer,
    }

    model_params = {
        "task_dict": task_dict,
        "robust": robust,
        "n_targets": n_targets,
        "elem_emb_len": elem_emb_len,
        "nbr_fea_len": nbr_fea_len,
        "elem_fea_len": elem_fea_len,
        "n_graph": n_graph,
        "h_fea_len": h_fea_len,
        "n_hidden": n_hidden,
    }

    os.makedirs(f"models/{model_name}", exist_ok=True)

    train_ensemble(
        model_class=CrystalGraphConvNet,
        model_name=model_name,
        run_id=run_id,
        ensemble_folds=ensemble,
        epochs=epochs,
        train_set=train_set,
        val_set=val_set,
        log=log,
        data_params=data_params,
        setup_params=setup_params,
        restart_params=restart_params,
        model_params=model_params,
        loss_dict=loss_dict,
    )

    data_params["batch_size"] = 64 * batch_size  # faster model inference
    data_params["shuffle"] = False  # need fixed data order due to ensembling

    results_dict = results_multitask(
        model_class=CrystalGraphConvNet,
        model_name=model_name,
        run_id=run_id,
        ensemble_folds=ensemble,
        test_set=test_set,
        data_params=data_params,
        robust=robust,
        task_dict=task_dict,
        device=device,
        eval_type="checkpoint",
        save_results=False,
    )

    pred = results_dict["last phdos peak"]["pred"]
    target = results_dict["last phdos peak"]["target"]

    y_ens = np.mean(pred, axis=0)

    mae = np.abs(target - y_ens).mean()
    mse = np.square(target - y_ens).mean()
    rmse = np.sqrt(mse)
    r2 = r2_score(target, y_ens)

    assert r2 > 0.7
    assert mae < 150
    assert rmse < 300


if __name__ == "__main__":
    test_cgcnn_regression()