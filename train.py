"""Trainining script for Tacotron speech synthesis model.

usage: train.py [options]

options:
    --data-root=<dir>         Directory contains preprocessed features.
    --checkpoint-dir=<dir>    Directory where to save model checkpoints [default: checkpoints].
    --checkpoint-path=<name>  Restore model from checkpoint path if given.
    --hparams=<parmas>        Hyper parameters [default: ].
    --log-event-path=<name>     Log event path.
    --reset-optimizer         Reset optimizer.
    -h, --help                Show this help message and exit
"""
from docopt import docopt

import sys
from os.path import dirname, join
from tqdm import tqdm, trange
from datetime import datetime

# The deepvoice3 model
from deepvoice3_pytorch import frontend, build_deepvoice3
import audio
import lrschedule

import torch
from torch.utils import data as data_utils
from torch.autograd import Variable
from torch import nn
from torch import optim
import torch.backends.cudnn as cudnn
from torch.utils import data as data_utils
import numpy as np
from numba import jit

from nnmnkwii.datasets import FileSourceDataset, FileDataSource
from os.path import join, expanduser

import librosa.display
from matplotlib import pyplot as plt
import sys
import os
from tensorboardX import SummaryWriter
from matplotlib import cm
from hparams import hparams, hparams_debug_string

fs = hparams.sample_rate

global_step = 0
global_epoch = 0
use_cuda = torch.cuda.is_available()
if use_cuda:
    cudnn.benchmark = True

_frontend = None  # to be set later


def _pad(seq, max_len, constant_values=0):
    return np.pad(seq, (0, max_len - len(seq)),
                  mode='constant', constant_values=constant_values)


def _pad_2d(x, max_len, b_pad=0):
    x = np.pad(x, [(b_pad, max_len - len(x) - b_pad), (0, 0)],
               mode="constant", constant_values=0)
    return x


def plot_alignment(alignment, path, info=None):
    fig, ax = plt.subplots()
    im = ax.imshow(
        alignment,
        aspect='auto',
        origin='lower',
        interpolation='none')
    fig.colorbar(im, ax=ax)
    xlabel = 'Decoder timestep'
    if info is not None:
        xlabel += '\n\n' + info
    plt.xlabel(xlabel)
    plt.ylabel('Encoder timestep')
    plt.tight_layout()
    plt.savefig(path, format='png')


class TextDataSource(FileDataSource):
    def __init__(self, data_root):
        self.data_root = data_root

    def collect_files(self):
        meta = join(self.data_root, "train.txt")
        with open(meta, "rb") as f:
            lines = f.readlines()
        lines = list(map(lambda l: l.decode("utf-8").split("|")[-1], lines))
        return lines

    def collect_features(self, text):
        seq = _frontend.text_to_sequence(text, p=hparams.replace_pronunciation_prob)
        return np.asarray(seq, dtype=np.int32)


class _NPYDataSource(FileDataSource):
    def __init__(self, data_root, col):
        self.data_root = data_root
        self.col = col

    def collect_files(self):
        meta = join(self.data_root, "train.txt")
        with open(meta, "rb") as f:
            lines = f.readlines()
        lines = list(map(lambda l: l.decode("utf-8").split("|")[self.col], lines))
        paths = list(map(lambda f: join(self.data_root, f), lines))
        return paths

    def collect_features(self, path):
        return np.load(path)


class MelSpecDataSource(_NPYDataSource):
    def __init__(self, data_root):
        super(MelSpecDataSource, self).__init__(data_root, 1)


class LinearSpecDataSource(_NPYDataSource):
    def __init__(self, data_root):
        super(LinearSpecDataSource, self).__init__(data_root, 0)


class PyTorchDataset(object):
    def __init__(self, X, Mel, Y):
        self.X = X
        self.Mel = Mel
        self.Y = Y

    def __getitem__(self, idx):
        return self.X[idx], self.Mel[idx], self.Y[idx]

    def __len__(self):
        return len(self.X)


def sequence_mask(sequence_length, max_len=None):
    if max_len is None:
        max_len = sequence_length.data.max()
    batch_size = sequence_length.size(0)
    seq_range = torch.arange(0, max_len).long()
    seq_range_expand = seq_range.unsqueeze(0).expand(batch_size, max_len)
    seq_range_expand = Variable(seq_range_expand)
    if sequence_length.is_cuda:
        seq_range_expand = seq_range_expand.cuda()
    seq_length_expand = sequence_length.unsqueeze(1) \
        .expand_as(seq_range_expand)
    return (seq_range_expand < seq_length_expand).float()


class MaskedL1Loss(nn.Module):
    def __init__(self):
        super(MaskedL1Loss, self).__init__()
        self.criterion = nn.L1Loss(size_average=False)

    def forward(self, input, target, lengths=None, mask=None, max_len=None):
        if lengths is None and mask is None:
            raise RuntimeError("Should provide either lengths or mask")

        # (B, T, 1)
        if mask is None:
            mask = sequence_mask(lengths, max_len).unsqueeze(-1)

        # (B, T, D)
        mask_ = mask.expand_as(input)
        loss = self.criterion(input * mask_, target * mask_)
        return loss / mask_.sum()


def collate_fn(batch):
    """Create batch"""
    r = hparams.outputs_per_step
    downsample_step = hparams.downsample_step

    # Lengths
    input_lengths = [len(x[0]) for x in batch]
    max_input_len = max(input_lengths)

    target_lengths = [len(x[1]) for x in batch]

    max_target_len = max(target_lengths)
    if max_target_len % r != 0:
        max_target_len += r - max_target_len % r
        assert max_target_len % r == 0
    if max_target_len % downsample_step != 0:
        max_target_len += downsample_step - max_target_len % downsample_step
        assert max_target_len % downsample_step == 0

    # Set 0 for zero beginning padding
    # imitates initial decoder states
    b_pad = r
    max_target_len += b_pad * downsample_step

    a = np.array([_pad(x[0], max_input_len) for x in batch], dtype=np.int)
    x_batch = torch.LongTensor(a)

    input_lengths = torch.LongTensor(input_lengths)
    target_lengths = torch.LongTensor(target_lengths)

    b = np.array([_pad_2d(x[1], max_target_len, b_pad=b_pad) for x in batch],
                 dtype=np.float32)
    mel_batch = torch.FloatTensor(b)

    c = np.array([_pad_2d(x[2], max_target_len, b_pad=b_pad) for x in batch],
                 dtype=np.float32)
    y_batch = torch.FloatTensor(c)

    # text positions
    text_positions = np.array([_pad(np.arange(1, len(x[0]) + 1), max_input_len)
                               for x in batch], dtype=np.int)
    text_positions = torch.LongTensor(text_positions)

    max_decoder_target_len = max_target_len // r // downsample_step

    # frame positions
    s, e = 1, max_decoder_target_len + 1
    # if b_pad > 0:
    #    s, e = s - 1, e - 1
    frame_positions = torch.arange(s, e).long().unsqueeze(0).expand(
        len(batch), max_decoder_target_len)

    # done flags
    done = np.array([_pad(np.zeros(len(x[1]) // r // downsample_step - 1),
                          max_decoder_target_len, constant_values=1)
                     for x in batch])
    done = torch.FloatTensor(done).unsqueeze(-1)

    return x_batch, input_lengths, mel_batch, y_batch, \
        (text_positions, frame_positions), done, target_lengths


def save_alignment(path, attn):
    plot_alignment(attn.T, path, info="deepvoice3, step={}".format(global_step))


def prepare_spec_image(spectrogram):
    # [0, 1]
    spectrogram = (spectrogram - np.min(spectrogram)) / (np.max(spectrogram) - np.min(spectrogram))
    spectrogram = np.flip(spectrogram, axis=1)  # flip against freq axis
    return np.uint8(cm.magma(spectrogram.T) * 255)


def save_states(global_step, writer, mel_outputs, linear_outputs, attn, mel, y,
                input_lengths, checkpoint_dir=None):
    print("Save intermediate states at step {}".format(global_step))

    # idx = np.random.randint(0, len(input_lengths))
    idx = min(1, len(input_lengths) - 1)
    input_length = input_lengths[idx]

    # Alignment
    # Multi-hop attention
    if attn.dim() == 4:
        for i, alignment in enumerate(attn):
            alignment = alignment[idx].cpu().data.numpy()
            tag = "alignment_layer{}".format(i + 1)
            writer.add_image(tag, np.uint8(cm.viridis(np.flip(alignment, 1).T) * 255), global_step)

            # save files as well for now
            alignment_dir = join(checkpoint_dir, "alignment_layer{}".format(i + 1))
            os.makedirs(alignment_dir, exist_ok=True)
            path = join(alignment_dir, "step{:09d}_layer_{}_alignment.png".format(
                global_step, i + 1))
            save_alignment(path, alignment)

        # Save averaged alignment
        alignment_dir = join(checkpoint_dir, "alignment_ave")
        os.makedirs(alignment_dir, exist_ok=True)
        path = join(alignment_dir, "step{:09d}_alignment.png".format(global_step))
        alignment = attn.mean(0)[idx].cpu().data.numpy()
        save_alignment(path, alignment)

        tag = "averaged_alignment"
        writer.add_image(tag, np.uint8(cm.viridis(np.flip(alignment, 1).T) * 255), global_step)
    else:
        assert False

    # Predicted mel spectrogram
    mel_output = mel_outputs[idx].cpu().data.numpy()
    mel_output = prepare_spec_image(audio._denormalize(mel_output))
    writer.add_image("Predicted mel spectrogram", mel_output, global_step)

    # Predicted spectrogram
    linear_output = linear_outputs[idx].cpu().data.numpy()
    spectrogram = prepare_spec_image(audio._denormalize(linear_output))
    writer.add_image("Predicted linear spectrogram", spectrogram, global_step)

    # Predicted audio signal
    signal = audio.inv_spectrogram(linear_output.T)
    signal /= np.max(np.abs(signal))
    path = join(checkpoint_dir, "step{:09d}_predicted.wav".format(
        global_step))
    try:
        writer.add_audio("Predicted audio signal", signal, global_step, sample_rate=fs)
    except:
        # TODO:
        pass
    audio.save_wav(signal, path)

    # Target mel spectrogram
    mel_output = mel[idx].cpu().data.numpy()
    mel_output = prepare_spec_image(audio._denormalize(mel_output))
    writer.add_image("Target mel spectrogram", mel_output, global_step)

    # Target spectrogram
    linear_output = y[idx].cpu().data.numpy()
    spectrogram = prepare_spec_image(audio._denormalize(linear_output))
    writer.add_image("Target linear spectrogram", spectrogram, global_step)


def logit(x, eps=1e-8):
    return torch.log(x + eps) - torch.log(1 - x + eps)


def masked_mean(y, mask):
    # (B, T, D)
    mask_ = mask.expand_as(y)
    return (y * mask_).sum() / mask_.sum()


def spec_loss(y_hat, y, mask, priority_bin=None, priority_w=0):
    masked_l1 = MaskedL1Loss()
    l1 = nn.L1Loss()
    l1_loss = 0.5 * masked_l1(y_hat, y, mask=mask) + 0.5 * l1(y_hat, y)
    if priority_bin is not None and priority_w > 0:
        priority_loss = 0.5 * masked_l1(
            y_hat[:, :, :priority_bin], y[:, :, :priority_bin], mask=mask) \
            + 0.5 * l1(y_hat[:, :, :priority_bin], y[:, :, :priority_bin])
        l1_loss = (1 - priority_w) * l1_loss + priority_w * priority_loss

    if hparams.binary_divergence_weight <= 0:
        binary_div = Variable(y.data.new(1).zero_())
    else:
        # TODO: I cannot get good results with this yet
        y_hat_logits = logit(y_hat)
        z = -y * y_hat_logits + torch.log(1 + torch.exp(y_hat_logits))
        binary_div = 0.5 * masked_mean(z, mask) + 0.5 * z.mean()

    return l1_loss, binary_div


@jit(nopython=True)
def guided_attention(N, max_N, T, max_T, g):
    W = np.zeros((max_N, max_T), dtype=np.float32)
    for n in range(N):
        for t in range(T):
            W[n, t] = 1 - np.exp(-(n / N - t / T)**2 / (2 * g * g))
    return W


def guided_attentions(input_lengths, target_lengths, max_target_len, g=0.2):
    B = len(input_lengths)
    max_input_len = input_lengths.max()
    W = np.zeros((B, max_target_len, max_input_len), dtype=np.float32)
    for b in range(B):
        W[b] = guided_attention(input_lengths[b], max_input_len,
                                target_lengths[b], max_target_len, g).T
    return W


def train(model, data_loader, optimizer, writer,
          init_lr=0.002,
          checkpoint_dir=None, checkpoint_interval=None, nepochs=None,
          clip_thresh=1.0):
    model.train()
    if use_cuda:
        model = model.cuda()
    linear_dim = model.linear_dim
    r = hparams.outputs_per_step
    downsample_step = hparams.downsample_step
    current_lr = init_lr

    binary_criterion = nn.BCELoss()

    global global_step, global_epoch
    while global_epoch < nepochs:
        running_loss = 0.
        for step, (x, input_lengths, mel, y, positions, done, target_lengths) \
                in tqdm(enumerate(data_loader)):
            # Learning rate schedule
            if hparams.lr_schedule is not None:
                lr_schedule_f = getattr(lrschedule, hparams.lr_schedule)
                current_lr = lr_schedule_f(
                    init_lr, global_step, **hparams.lr_schedule_kwargs)
                for param_group in optimizer.param_groups:
                    param_group['lr'] = current_lr
            optimizer.zero_grad()

            # Used for Position encoding
            text_positions, frame_positions = positions

            # Downsample mel spectrogram
            if downsample_step > 1:
                mel = mel[:, 0::downsample_step, :]

            # Lengths
            input_lengths = input_lengths.long().numpy()
            decoder_lengths = target_lengths.long().numpy() // r // downsample_step

            # Feed data
            x, mel, y = Variable(x), Variable(mel), Variable(y)
            text_positions = Variable(text_positions)
            frame_positions = Variable(frame_positions)
            done = Variable(done)
            target_lengths = Variable(target_lengths)
            if use_cuda:
                x, mel, y = x.cuda(), mel.cuda(), y.cuda()
                text_positions = text_positions.cuda()
                frame_positions = frame_positions.cuda()
                done, target_lengths = done.cuda(), target_lengths.cuda()

            # decoder output domain mask
            decoder_target_mask = sequence_mask(
                target_lengths / (r * downsample_step),
                max_len=mel.size(1)).unsqueeze(-1)
            if downsample_step > 1:
                # spectrogram-domain mask
                target_mask = sequence_mask(
                    target_lengths, max_len=y.size(1)).unsqueeze(-1)
            else:
                target_mask = decoder_target_mask

            mel_outputs, linear_outputs, attn, done_hat = model(
                x, mel,
                text_positions=text_positions, frame_positions=frame_positions,
                input_lengths=input_lengths)

            # Losses
            w = hparams.binary_divergence_weight

            # mel:
            mel_l1_loss, mel_binary_div = spec_loss(
                mel_outputs[:, :-r, :], mel[:, r:, :], decoder_target_mask[:, r:, :])
            mel_loss = (1 - w) * mel_l1_loss + w * mel_binary_div

            # done:
            done_loss = binary_criterion(done_hat, done)

            # linear:
            n_priority_freq = int(hparams.priority_freq / (fs * 0.5) * linear_dim)
            linear_l1_loss, linear_binary_div = spec_loss(
                linear_outputs[:, :-r, :], y[:, r:, :], target_mask[:, r:, :],
                priority_bin=n_priority_freq,
                priority_w=hparams.priority_freq_weight)
            linear_loss = (1 - w) * linear_l1_loss + w * linear_binary_div

            loss = mel_loss + linear_loss + done_loss

            # attention
            if hparams.use_guided_attention:
                soft_mask = guided_attentions(input_lengths, decoder_lengths,
                                              attn.size(-2),
                                              g=hparams.guided_attention_sigma)
                soft_mask = Variable(torch.from_numpy(soft_mask))
                soft_mask = soft_mask.cuda() if use_cuda else soft_mask
                attn_loss = (attn * soft_mask).mean()
                loss += attn_loss

            if global_step > 0 and global_step % checkpoint_interval == 0:
                save_states(
                    global_step, writer, mel_outputs, linear_outputs, attn,
                    mel, y, input_lengths, checkpoint_dir)
                save_checkpoint(
                    model, optimizer, global_step, checkpoint_dir, global_epoch)

            # Update
            loss.backward()
            if clip_thresh > 0:
                grad_norm = torch.nn.utils.clip_grad_norm(
                    model.get_trainable_parameters(), clip_thresh)
            optimizer.step()

            # Logs
            writer.add_scalar("loss", float(loss.data[0]), global_step)
            writer.add_scalar("done_loss", float(done_loss.data[0]), global_step)
            writer.add_scalar("mel loss", float(mel_loss.data[0]), global_step)
            writer.add_scalar("mel_l1_loss", float(mel_l1_loss.data[0]), global_step)
            writer.add_scalar("mel_binary_div", float(mel_binary_div.data[0]), global_step)
            writer.add_scalar("linear_loss", float(linear_loss.data[0]), global_step)
            writer.add_scalar("linear_l1_loss", float(linear_l1_loss.data[0]), global_step)
            writer.add_scalar("linear_binary_div", float(linear_binary_div.data[0]), global_step)
            if hparams.use_guided_attention:
                writer.add_scalar("attn_loss", float(attn_loss.data[0]), global_step)

            if clip_thresh > 0:
                writer.add_scalar("gradient norm", grad_norm, global_step)
            writer.add_scalar("learning rate", current_lr, global_step)

            global_step += 1
            running_loss += loss.data[0]

        averaged_loss = running_loss / (len(data_loader))
        writer.add_scalar("loss (per epoch)", averaged_loss, global_epoch)
        print("Loss: {}".format(running_loss / (len(data_loader))))

        global_epoch += 1


def save_checkpoint(model, optimizer, step, checkpoint_dir, epoch):
    checkpoint_path = join(
        checkpoint_dir, "checkpoint_step{:09d}.pth".format(global_step))
    torch.save({
        "state_dict": model.state_dict(),
        "optimizer": optimizer.state_dict(),
        "global_step": step,
        "global_epoch": epoch,
    }, checkpoint_path)
    print("Saved checkpoint:", checkpoint_path)


def build_model():
    model = build_deepvoice3(n_vocab=_frontend.n_vocab,
                             embed_dim=hparams.text_embed_dim,
                             mel_dim=hparams.num_mels,
                             linear_dim=hparams.fft_size // 2 + 1,
                             r=hparams.outputs_per_step,
                             padding_idx=hparams.padding_idx,
                             dropout=hparams.dropout,
                             kernel_size=hparams.kernel_size,
                             encoder_channels=hparams.encoder_channels,
                             decoder_channels=hparams.decoder_channels,
                             converter_channels=hparams.converter_channels,
                             use_memory_mask=hparams.use_memory_mask,
                             trainable_positional_encodings=hparams.trainable_positional_encodings
                             )
    return model


if __name__ == "__main__":
    args = docopt(__doc__)
    print("Command line args:\n", args)
    checkpoint_dir = args["--checkpoint-dir"]
    checkpoint_path = args["--checkpoint-path"]
    data_root = args["--data-root"]
    if data_root is None:
        data_root = join(expanduser("~"), "data", "ljspeech")

    log_event_path = args["--log-event-path"]
    reset_optimizer = args["--reset-optimizer"]

    # Override hyper parameters
    hparams.parse(args["--hparams"])
    assert hparams.name == "deepvoice3"

    _frontend = getattr(frontend, hparams.frontend)

    os.makedirs(checkpoint_dir, exist_ok=True)

    # Input dataset definitions
    X = FileSourceDataset(TextDataSource(data_root))
    Mel = FileSourceDataset(MelSpecDataSource(data_root))
    Y = FileSourceDataset(LinearSpecDataSource(data_root))

    # Dataset and Dataloader setup
    dataset = PyTorchDataset(X, Mel, Y)
    data_loader = data_utils.DataLoader(
        dataset, batch_size=hparams.batch_size,
        num_workers=hparams.num_workers, shuffle=True,
        collate_fn=collate_fn, pin_memory=hparams.pin_memory)

    # Model
    model = build_model()

    optimizer = optim.Adam(model.get_trainable_parameters(),
                           lr=hparams.initial_learning_rate, betas=(
        hparams.adam_beta1, hparams.adam_beta2),
        eps=hparams.adam_eps, weight_decay=hparams.weight_decay)

    # Load checkpoint
    if checkpoint_path:
        print("Load checkpoint from: {}".format(checkpoint_path))
        checkpoint = torch.load(checkpoint_path)
        model.load_state_dict(checkpoint["state_dict"])
        if not reset_optimizer:
            optimizer.load_state_dict(checkpoint["optimizer"])
        global_step = checkpoint["global_step"]
        global_epoch = checkpoint["global_epoch"]

    # Setup summary writer for tensorboard
    if log_event_path is None:
        log_event_path = "log/run-test" + str(datetime.now()).replace(" ", "_")
    print("Los event path: {}".format(log_event_path))
    writer = SummaryWriter(log_dir=log_event_path)

    print(hparams_debug_string())

    # Train!
    try:
        train(model, data_loader, optimizer, writer,
              init_lr=hparams.initial_learning_rate,
              checkpoint_dir=checkpoint_dir,
              checkpoint_interval=hparams.checkpoint_interval,
              nepochs=hparams.nepochs,
              clip_thresh=hparams.clip_thresh)
    except KeyboardInterrupt:
        save_checkpoint(
            model, optimizer, global_step, checkpoint_dir, global_epoch)

    print("Finished")
    sys.exit(0)
