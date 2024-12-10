import fairseq
import torch
import torch.nn as nn
import pandas as pd
import os
import numpy as np
from tqdm import tqdm
from scipy.spatial.distance import cdist
from urllib.request import urlretrieve
import torchaudio
import torch.nn.functional as F
from datetime import datetime


class Nomad():
    def __init__(self, device=None, cache_dir="./nomad_pt-models"):

        # *** DEVICE SETTINGS ***
        # Automatically set based on GPU detection
        if torch.cuda.is_available():
            self.DEVICE = "cuda"
        else:
            self.DEVICE = "cpu"

        # Overwrite user choice
        if device is not None:
            self.DEVICE = device

        print(f"NOMAD running on: {self.DEVICE}")

        # *** LOAD MODEL ***
        # *** Pytorch models download options ****
        if not os.path.isdir(cache_dir):
            print("Creating pt-models directory")
            os.makedirs(cache_dir)

        # Download wav2vec 2.0 base
        url_w2v = "https://dl.fbaipublicfiles.com/fairseq/wav2vec/wav2vec_small.pt"
        w2v_path = "{}/wav2vec_small.pt".format(cache_dir)
        if not os.path.isfile(w2v_path):
            print("Downloading wav2vec 2.0 started")
            urlretrieve(url_w2v, w2v_path)
            print("wav2vec 2.0 download completed")

        # w2v BASE parameters
        CHECKPOINT_PATH = w2v_path
        SSL_OUT_DIM = 768
        EMB_DIM = 256

        # Load w2v BASE
        w2v_model, _, _ = fairseq.checkpoint_utils.load_model_ensemble_and_task(
            [CHECKPOINT_PATH]
        )
        ssl_model = w2v_model[0]
        ssl_model.remove_pretraining_modules()

        # Download NOMAD
        url_nomad_db = "https://www.dropbox.com/scl/fi/uws3wk327adbwqo22cr0p/nomad_best_model.pt?rlkey=cco21iba6xxi81a0dm9lpa7zj&dl=1"
        nomad_path = "{}/nomad_best_model.pt".format(cache_dir)
        if not os.path.isfile(nomad_path):
            print("Downloading NOMAD weights started")
            urlretrieve(url_nomad_db, nomad_path)
            print("NOMAD weights download completed")

        # Create NOMAD model
        model = TripletModel(ssl_model, SSL_OUT_DIM, EMB_DIM)
        MODEL_PATH = nomad_path
        model.load_state_dict(torch.load(MODEL_PATH, map_location=self.DEVICE))
        self.model = model
        self.model.to(self.DEVICE)
        self.model.eval()

        # Create NOMAD loss model
        self.lossnet_layers = LossNetLayers(ssl_model, SSL_OUT_DIM, EMB_DIM)
        self.lossnet_layers.to(self.DEVICE)

        # Freeze update (no need to update gradient only propagate) -> Needs test
        # for p in self.lossnet_layers.parameters():
        #    p.requires_grad=False

        self.nomad_loss = NomadLoss()
        self.nomad_loss.to(self.DEVICE)
        self.nomad_loss.eval()

    def predict(
        self, mode="dir", nmr="data/nmr-data", deg="data/test-data", results_path=None
    ):
        if nmr is None:
            raise Exception(
                "nmr_path not specified, you need to pass a valid value to nmr_path"
            )
        if deg is None:
            raise Exception(
                "test_path not specified, you need to pass a valide value to test_path"
            )

        if mode == "dir":
            if os.path.isdir(nmr) == False:
                raise Exception(
                    f"Path to the non-matching reference files {nmr} does not exist"
                )
            if os.path.isdir(deg) == False:
                raise Exception(f"Path to the test files {deg} does not exist")
        elif mode == "csv":
            if os.path.isfile(nmr) == False:
                raise Exception(f"File {nmr} does not exist")
            if os.path.isfile(deg) == False:
                raise Exception(f"File {deg} does not exist")
        else:
            raise Exception(
                f"Mode value {mode} is not valid. Valid values are dir and csv"
            )

        print(f"Compute non-matching reference embeddings from {nmr}")
        nmr_embeddings = self.get_embeddings(nmr).set_index("filename")

        print(f"Compute degraded embeddings from {deg}")
        test_embeddings = self.get_embeddings(deg).set_index("filename")

        # Compute pairwise distance matrix
        distance_matrix = cdist(test_embeddings, nmr_embeddings)

        # Compute average NOMAD score
        avg_nomad = np.mean(distance_matrix, axis=1)

        # Return both distance matrix and mean nomad score
        test_files = [x.split("/")[-1].split(".")[0] for x in test_embeddings.index]
        df_avg_nomad = (
            pd.DataFrame({"Test File": test_files, "NOMAD": avg_nomad})
            .set_index("Test File")
            .round(3)
        )
        """
        df_dm = pd.DataFrame(distance_matrix).round(3)
        df_dm["Test File"] = test_files
        df_dm.set_index("Test File", inplace=True)
        df_dm.columns = [x.split("/")[-1].split(".")[0] for x in nmr_embeddings.index]

        # Save results
        if results_path == None:
            now = datetime.now()
            dt_string = now.strftime("%d-%m-%Y_%H-%M-%S")
            results_avg_path = os.path.join("results-csv", dt_string)
            if os.path.isdir(results_avg_path) == False:
                os.makedirs(results_avg_path)
            results_scores_path = os.path.join("results-csv", dt_string)
            if os.path.isdir(results_scores_path) == False:
                os.makedirs(results_scores_path)
            results_avg_path = os.path.join(
                results_avg_path, f"{dt_string}_nomad_avg.csv"
            )
            results_scores_path = os.path.join(
                results_scores_path, f"{dt_string}_nomad_scores.csv"
            )
        else:
            results_avg_path = os.path.join(results_path, "nomad_avg.csv")
            results_scores_path = os.path.join(results_path, "nomad_scores.csv")

        df_avg_nomad.reset_index().to_csv(results_avg_path, index=False)
        df_dm.reset_index().to_csv(results_scores_path, index=False)
        """
        return df_avg_nomad

    def forward(self, estimate, clean):
        estimate_embeddings = self.lossnet_layers(estimate)
        clean_embeddings = self.lossnet_layers(clean)
        loss = self.nomad_loss(clean_embeddings, estimate_embeddings)
        return loss

    def get_embeddings(self, path):
        # If mode == dir
        if os.path.isdir(path):
            # Dataframe
            data = pd.DataFrame(os.listdir(path))
            data.columns = ["filename"]
            data["filename"] = [os.path.join(path, x) for x in data["filename"]]
        # If mode == csv
        elif os.path.isfile(path):
            data = pd.read_csv(path)
            if "filename" not in data.columns:
                raise Exception(
                    "File {path} not including a column called filename. Please pass a csv file with a column called filename that includes the absolute filpaths of the waveforms."
                )

        # Calculate embeddings
        embeddings = self.get_embeddings_csv(self.model, data)
        return embeddings

    # Function that extract NOMAD embeddings and store them in a csv
    def get_embeddings_csv(self, model, file_names, root=False):
        file_names_arr = np.array(file_names)
        embeddings = []

        model.eval()
        with torch.no_grad():
            for i, filename_anchor in enumerate(tqdm(file_names_arr)):
                if root:
                    filepath = os.path.join(root, filename_anchor)
                else:
                    filepath = filename_anchor

                wave = self.load_processing_versa(filepath, trim=False)
                lengths = None

                wave = wave.to(self.DEVICE)
                nomad_embeddings = model(wave, lengths)
                embeddings.append(nomad_embeddings.squeeze().cpu().detach().numpy())

            embeddings = np.array(embeddings)
            embeddings = pd.DataFrame(embeddings)
            df_emb = pd.concat([file_names.reset_index(), embeddings], axis=1).drop(
                "index", axis=1
            )

        return df_emb

    # Load wave file
    def load_processing_versa(self, source_wav, target_sr=16000, trim=False):
        """
        Loads and preprocesses an audio numpy array.

        Args:
            source_wav: Aa numpy array containing the audio data.
            target_sr: Target sample rate (default: 16000 Hz).
            trim: Whether to trim the audio to 10 seconds (default: False).

        Returns:
            The preprocessed audio waveform as a PyTorch tensor.
        """

        # Load waveform
        wave = torch.tensor(source_wav).unsqueeze(0).float()

        # Trim audio to 10 secs
        if trim:
            if wave.shape[1] > target_sr * 10:
                wave = wave[:, : target_sr * 10]

        return wave


class TripletModel(nn.Module):
    def __init__(self, ssl_model, ssl_out_dim, emb_dim=256):
        super(TripletModel, self).__init__()
        self.ssl_model = ssl_model
        self.ssl_features = ssl_out_dim
        self.embedding_layer = nn.Sequential(
            nn.ReLU(), nn.Linear(self.ssl_features, emb_dim)
        )

    def forward(self, wav, lengths=None):
        wav = wav.squeeze(1)  ## [batches, audio_len]
        res = self.ssl_model(wav, mask=False, features_only=True)
        x = res["x"]
        x_tr = torch.mean(x, 1)
        x = self.embedding_layer(x_tr)
        x = torch.nn.functional.normalize(x, dim=1)
        return x


class LossNetLayers(nn.Module):
    def __init__(self, ssl_model, ssl_out_dim, emb_dim=256):
        super(LossNetLayers, self).__init__()
        self.ssl_model = ssl_model
        self.ssl_features = ssl_out_dim
        self.embedding_layer = nn.Sequential(
            nn.ReLU(), nn.Linear(self.ssl_features, emb_dim)
        )

    def forward(self, wav):
        wav = wav.squeeze(1)
        res = self.ssl_model(wav, mask=False, features_only=True)

        # Get Transformer layers
        lossnet_layers = [x[0].permute(1, 0, 2) for x in res["layer_results"]]

        # Get embedding layers
        x = res["x"]
        x_tr = torch.mean(x, 1)
        x = self.embedding_layer(x_tr)
        x = torch.nn.functional.normalize(x, dim=1)

        # Append layers
        lossnet_layers.append(x)
        return lossnet_layers


class NomadLoss(nn.Module):
    def __init__(self):
        super(NomadLoss, self).__init__()
        # How many layers to use (12 transformer + 1 embedding)
        self.L = 13
        self.only_embedding = False

    def forward(self, nomad_ref, nomad_test):
        l1_dist = 0.0

        if self.only_embedding:
            ref = nomad_ref[13]
            test = nomad_test[13]
            l1_dist = F.l1_loss(test, ref)
        else:
            # Loop over each layer and calculate L1 distance
            for i in range(self.L):
                ref = nomad_ref[i]
                test = nomad_test[i]

                # Calculate L1 loss (transformer layers -> loss in each time frame)
                l1_dist += F.l1_loss(test, ref)
        return l1_dist
