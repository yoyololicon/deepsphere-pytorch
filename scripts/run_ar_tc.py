"""Example script for running DeepSphere U-Net on reduced AR_TC dataset.
"""

import numpy as np
import torch
from torch import nn, optim
from torch.optim.lr_scheduler import ReduceLROnPlateau, StepLR
from torch.utils.tensorboard import SummaryWriter

from ignite.contrib.handlers.param_scheduler import create_lr_scheduler_with_warmup
from ignite.contrib.handlers.tensorboard_logger import GradsHistHandler, OptimizerParamsHandler, OutputHandler, \
    TensorboardLogger, WeightsHistHandler
from ignite.engine import Engine, Events, create_supervised_evaluator
from ignite.handlers import EarlyStopping, TerminateOnNan
from ignite.metrics import EpochMetric, RunningAverage
from ignite.contrib.handlers import ProgressBar
from ignite.utils import convert_tensor

from sklearn.model_selection import train_test_split
from torch_geometric.data import DenseDataLoader

from deepsphere.data.datasets.dataset import ARTCDataset, ARTCH5Dataset
from deepsphere.data.transforms.transforms import Normalize
from deepsphere.models.spherical_unet.unet_model import SphericalUNet
from deepsphere.utils.initialization import init_device
from deepsphere.utils.parser import create_parser, parse_config
from deepsphere.utils.stats_extractor import stats_extractor


def average_precision_compute_fn(y_pred, y_true):
    """Attached function to the custom ignite metric AveragePrecisionMultiLabel

    Args:
        y_pred (:obj:`torch.Tensor`): model predictions
        y_true (:obj:`torch.Tensor`): ground truths

    Raises:
        RuntimeError: Indicates that sklearn should be installed by the user.

    Returns:
        :obj:`numpy.array`: average precision vector.
                            Of the same length as the number of labels present in the data
    """
    try:
        from sklearn.metrics import average_precision_score
    except ImportError:
        raise RuntimeError("This metric requires sklearn to be installed.")

    ap = average_precision_score(y_true.numpy(), y_pred.numpy(), None)
    return ap


# Pylint and Ignite incompatibilities:
# pylint: disable=W0612
# pylint: disable=W0613


def validate_output_transform(x, y, y_pred):
    """A transform to format the output of the supervised evaluator before calculating the metric

    Args:
        x (:obj:`torch.Tensor`): the input to the model
        y (:obj:`torch.Tensor`): the output of the model
        y_pred (:obj:`torch.Tensor`): the ground truth labels

    Returns:
        (:obj:`torch.Tensor`, :obj:`torch.Tensor`): model predictions and ground truths reformatted
    """
    output = y_pred
    labels = y
    B, V, C = output.shape
    B_labels, V_labels, C_labels = labels.shape
    output = output.view(B * V, C)
    labels = labels.view(B_labels * V_labels, C_labels)
    return output, labels


def add_tensorboard(engine_train, optimizer, model, log_dir):
    """Creates an ignite logger object and adds training elements such as weight and gradient histograms

    Args:
        engine_train (:obj:`ignite.engine`): the train engine to attach to the logger
        optimizer (:obj:`torch.optim`): the model's optimizer
        model (:obj:`torch.nn.Module`): the model being trained
        log_dir (string): path to where tensorboard data should be saved
    """
    # Create a logger
    tb_logger = TensorboardLogger(log_dir=log_dir)

    # Attach the logger to the trainer to log training loss at each iteration
    tb_logger.attach(
        engine_train, log_handler=OutputHandler(tag="training", output_transform=lambda loss: {"loss": loss}),
        event_name=Events.ITERATION_COMPLETED
    )

    # Attach the logger to the trainer to log optimizer's parameters, e.g. learning rate at each iteration
    tb_logger.attach(engine_train, log_handler=OptimizerParamsHandler(optimizer), event_name=Events.EPOCH_COMPLETED)

    # Attach the logger to the trainer to log model's weights as a histogram after each epoch
    tb_logger.attach(engine_train, log_handler=WeightsHistHandler(model), event_name=Events.EPOCH_COMPLETED)

    # Attach the logger to the trainer to log model's gradients as a histogram after each epoch
    tb_logger.attach(engine_train, log_handler=GradsHistHandler(model), event_name=Events.EPOCH_COMPLETED)

    tb_logger.close()


def get_dataloaders(parser_args):
    """Creates the datasets and the corresponding dataloaders

    Args:
        parser_args (dict): parsed arguments

    Returns:
        (:obj:`torch.utils.data.dataloader`, :obj:`torch.utils.data.dataloader`): train, validation dataloaders
    """

    path_to_data = parser_args.path_to_data
    partition = parser_args.partition
    seed = parser_args.seed
    means_path = parser_args.means_path
    stds_path = parser_args.stds_path

    data = ARTCDataset(path_to_data)
    train_indices, temp = train_test_split(data.idxs, train_size=partition[0], random_state=seed)
    val_indices, _ = train_test_split(temp, test_size=partition[2] / (partition[1] + partition[2]), random_state=seed)

    if (means_path is None) or (stds_path is None):
        train_set_stats = ARTCDataset(path_to_data, indices=train_indices)
        means, stds = stats_extractor(train_set_stats)
        np.save("./means.npy", means)
        np.save("./stds.npy", stds)
    else:
        try:
            means = np.load(means_path)
            stds = np.load(stds_path)
        except ValueError:
            print("No means or stds were provided. Or path names incorrect.")

    train_set = ARTCDataset(
        path_to_data, indices=train_indices, transform=Normalize(means, stds)
    )
    validation_set = ARTCDataset(
        path_to_data, indices=val_indices, transform=Normalize(means, stds)
    )

    dataloader_train = DenseDataLoader(train_set, batch_size=parser_args.batch_size, shuffle=True, num_workers=1)
    dataloader_validation = DenseDataLoader(validation_set, batch_size=parser_args.batch_size, shuffle=False,
                                            num_workers=1)
    return dataloader_train, dataloader_validation


def main(parser_args):
    """Main function to create trainer engine, add handlers to train and validation engines.
    Then runs train engine to perform training and validation.

    Args:
        parser_args (dict): parsed arguments
    """
    dataloader_train, dataloader_validation = get_dataloaders(parser_args)
    criterion = nn.CrossEntropyLoss()

    unet = SphericalUNet(parser_args.pooling_class, parser_args.n_pixels, parser_args.depth, parser_args.laplacian_type,
                         parser_args.kernel_size)
    # unet = torch.jit.script(unet)
    unet, device = init_device(parser_args.device, unet)
    lr = parser_args.learning_rate
    optimizer = optim.Adam(unet.parameters(), lr=lr)
    print(sum(p.numel() for p in unet.parameters() if p.requires_grad))

    def trainer(engine, batch):
        """Train Function to define train engine.
        Called for every batch of the train engine, for each epoch.

        Args:
            engine (ignite.engine): train engine
            batch (:obj:`torch.utils.data.dataloader`): batch from train dataloader

        Returns:
            :obj:`torch.tensor` : train loss for that batch and epoch
        """
        unet.train()
        optimizer.zero_grad()

        data, labels = batch.x, batch.y
        labels = labels.to(device)
        data = data.to(device)
        output = unet(data)

        B, V, C = output.shape
        B_labels, V_labels, C_labels = labels.shape
        output = output.view(B * V, C)
        labels = labels.view(B_labels * V_labels, C_labels).max(1)[1]

        loss = criterion(output, labels)
        loss.backward()
        optimizer.step()
        return {'loss': loss.item()}

    writer = SummaryWriter(parser_args.tensorboard_path)

    engine_train = Engine(trainer)

    RunningAverage(output_transform=lambda x: x['loss']).attach(engine_train, 'loss')

    def prepare_batch(batch, device, non_blocking):
        """Prepare batch for training: pass to a device with options.

        """
        return (
            convert_tensor(batch.x, device=device, non_blocking=non_blocking),
            convert_tensor(batch.y, device=device, non_blocking=non_blocking),
        )

    engine_validate = create_supervised_evaluator(
        model=unet, metrics={"AP": EpochMetric(average_precision_compute_fn)}, device=device,
        output_transform=validate_output_transform,
        prepare_batch=prepare_batch
    )

    engine_train.add_event_handler(Events.EPOCH_STARTED, lambda x: print("Starting Epoch: {}".format(x.state.epoch)))
    engine_train.add_event_handler(Events.ITERATION_COMPLETED, TerminateOnNan())

    @engine_train.on(Events.EPOCH_COMPLETED)
    def epoch_validation(engine):
        """Handler to run the validation engine at the end of the train engine's epoch.

        Args:
            engine (ignite.engine): train engine
        """
        print("beginning validation epoch")
        engine_validate.run(dataloader_validation)

    reduce_lr_plateau = ReduceLROnPlateau(
        optimizer,
        mode=parser_args.reducelronplateau_mode,
        factor=parser_args.reducelronplateau_factor,
        patience=parser_args.reducelronplateau_patience,
    )

    @engine_validate.on(Events.EPOCH_COMPLETED)
    def update_reduce_on_plateau(engine):
        """Handler to reduce the learning rate on plateau at the end of the validation engine's epoch

        Args:
            engine (ignite.engine): validation engine
        """
        ap = engine.state.metrics["AP"]
        mean_average_precision = np.mean(ap[1:])
        reduce_lr_plateau.step(mean_average_precision)

    @engine_validate.on(Events.EPOCH_COMPLETED)
    def save_epoch_results(engine):
        """Handler to save the metrics at the end of the validation engine's epoch

        Args:
            engine (ignite.engine): validation engine
        """
        ap = engine.state.metrics["AP"]
        mean_average_precision = np.mean(ap[1:])
        print("Average precisions:", ap)
        print("mAP:", mean_average_precision)
        writer.add_scalars(
            "metrics",
            {"mean average precision (AR+TC)": mean_average_precision, "AR average precision": ap[2],
             "TC average precision": ap[1]},
            engine_train.state.epoch,
        )
        writer.close()

    step_scheduler = StepLR(optimizer, step_size=parser_args.steplr_step_size, gamma=parser_args.steplr_gamma)
    scheduler = create_lr_scheduler_with_warmup(
        step_scheduler,
        warmup_start_value=parser_args.warmuplr_warmup_start_value,
        warmup_end_value=parser_args.warmuplr_warmup_end_value,
        warmup_duration=parser_args.warmuplr_warmup_duration,
    )
    engine_validate.add_event_handler(Events.EPOCH_COMPLETED, scheduler)

    earlystopper = EarlyStopping(
        patience=parser_args.earlystopping_patience,
        score_function=lambda x: -x.state.metrics["AP"][1],
        trainer=engine_train
    )
    engine_validate.add_event_handler(Events.EPOCH_COMPLETED, earlystopper)

    add_tensorboard(engine_train, optimizer, unet, log_dir=parser_args.tensorboard_path)

    pbar = ProgressBar()
    pbar.attach(engine_train, metric_names=['loss'])

    engine_train.run(dataloader_train, max_epochs=parser_args.n_epochs)

    pbar.close()
    torch.save(unet.state_dict(), parser_args.model_save_path + "unet_state.pt")


if __name__ == "__main__":
    PARSER_ARGS = parse_config(create_parser())
    main(PARSER_ARGS)
