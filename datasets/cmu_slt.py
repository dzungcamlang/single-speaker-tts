import os

import librosa
import numpy as np

from audio.conversion import ms_to_samples, magnitude_to_decibel, normalize_decibel
from audio.features import linear_scale_spectrogram, mel_scale_spectrogram
from audio.io import load_wav
from datasets.dataset_helper import DatasetHelper
from datasets.statistics import collect_decibel_statistics
from tacotron.params.model import model_params


class CMUDatasetHelper(DatasetHelper):
    """
    Dataset loading helper for the CMU_ARCTIC dataset.
    """
    # Mel. scale spectrogram reference dB over the entire dataset.
    mel_mag_ref_db = 9.33

    # Mel. scale spectrogram maximum dB over the entire dataset.
    mel_mag_max_db = 100.0

    # Linear scale spectrogram reference dB over the entire dataset.
    linear_ref_db = 36.50

    # Linear scale spectrogram maximum dB over the entire dataset.
    linear_mag_max_db = 100.0

    # Raw waveform silence reference signal dB.
    raw_silence_db = None

    def __init__(self, dataset_folder, char_dict, fill_dict):
        super().__init__(dataset_folder, char_dict, fill_dict)

        self._abbreviations = {
            '.': ''
        }

    def load(self, max_samples=None, min_len=None, max_len=None, listing_file_name='train.txt'):
        data_file = os.path.join(self._dataset_folder, listing_file_name)
        wav_folder = os.path.join(self._dataset_folder, 'wav')

        file_paths = []
        sentences = []
        with open(data_file, 'r') as listing_file:
            # Iterate the metadata file.
            for line in listing_file:
                file_id, normalized_sentence = line.split(' ', maxsplit=1)

                # Remove new line characters.
                normalized_sentence = normalized_sentence.strip()

                # Extract the transcription.
                # We do not want the sentence to contain any non ascii characters.
                sentence = self.utf8_to_ascii(normalized_sentence)

                # Skip sentences in case they do not meet the length requirements.
                sentence_len = len(sentence)
                if min_len is not None:
                    if sentence_len < min_len:
                        continue

                # Skip sentences in case they do not meet the length requirements.
                if max_len is not None:
                    if sentence_len > max_len:
                        continue

                sentences.append(sentence)

                # Get the audio file path.
                file_path = '{}.wav'.format(os.path.join(wav_folder, file_id))
                file_paths.append(file_path)

                if max_samples is not None:
                    if len(sentences) == max_samples:
                        break

        # Normalize sentences, convert the characters to dictionary ids and determine their lengths.
        id_sentences, sentence_lengths = self.process_sentences(sentences)

        # for k, v in self._char2idx_dict.items():
        #     print("'{}': {},".format(k, v))

        return id_sentences, sentence_lengths, file_paths

    @staticmethod
    def load_audio(file_path):
        # Window length in audio samples.
        win_len = ms_to_samples(model_params.win_len, model_params.sampling_rate)
        # Window hop in audio samples.
        hop_len = ms_to_samples(model_params.win_hop, model_params.sampling_rate)

        # Load the actual audio file.
        wav, sr = load_wav(file_path.decode())

        # TODO: Determine a better silence reference level for the CMU_ARCTIC dataset (See: #9).
        # Remove silence at the beginning and end of the wav so the network does not have to learn
        # some random initial silence delay after which it is allowed to speak.
        wav, _ = librosa.effects.trim(wav)

        # Calculate the linear scale spectrogram.
        # Note the spectrogram shape is transposed to be (T_spec, 1 + n_fft // 2) so dense layers
        # for example are applied to each frame automatically.
        linear_spec = linear_scale_spectrogram(wav, model_params.n_fft, hop_len, win_len).T

        # Calculate the Mel. scale spectrogram.
        # Note the spectrogram shape is transposed to be (T_spec, n_mels) so dense layers for
        # example are applied to each frame automatically.
        mel_spec = mel_scale_spectrogram(wav, model_params.n_fft, sr, model_params.n_mels,
                                         model_params.mel_fmin, model_params.mel_fmax, hop_len,
                                         win_len, 1).T

        # Convert the linear spectrogram into decibel representation.
        linear_mag = np.abs(linear_spec)
        linear_mag_db = magnitude_to_decibel(linear_mag)
        linear_mag_db = normalize_decibel(linear_mag_db,
                                          CMUDatasetHelper.linear_ref_db,
                                          CMUDatasetHelper.linear_mag_max_db)
        # => linear_mag_db.shape = (T_spec, 1 + n_fft // 2)

        # Convert the mel spectrogram into decibel representation.
        mel_mag = np.abs(mel_spec)
        mel_mag_db = magnitude_to_decibel(mel_mag)
        mel_mag_db = normalize_decibel(mel_mag_db,
                                       CMUDatasetHelper.mel_mag_ref_db,
                                       CMUDatasetHelper.mel_mag_max_db)
        # => mel_mag_db.shape = (T_spec, n_mels)

        # Tacotron reduction factor.
        if model_params.reduction > 1:
            mel_mag_db, linear_mag_db = DatasetHelper.apply_reduction_padding(mel_mag_db,
                                                                              linear_mag_db,
                                                                              model_params.reduction)

        return np.array(mel_mag_db).astype(np.float32), \
               np.array(linear_mag_db).astype(np.float32)


if __name__ == '__main__':
    init_char_dict = {
        'pad': 0,  # padding
        'eos': 1,  # end of sequence
        'a': 2, 'u': 3, 't': 4, 'h': 5, 'o': 6, 'r': 7, ' ': 8, 'f': 9, 'e': 10, 'd': 11, 'n': 12,
        'g': 13, 'i': 14, 'l': 15, ',': 16, 'p': 17, 's': 18, 'c': 19, 'm': 20, 'z': 21, 'w': 22,
        'v': 23, 'k': 24, 'b': 25, "'": 26, 'y': 27, 'j': 28, 'q': 29, 'x': 30, '-': 31, ';': 32
    }

    dataset = CMUDatasetHelper(dataset_folder='/home/yves-noel/documents/master/thesis/datasets/cmu_us_slt_arctic',
                               char_dict=init_char_dict,
                               fill_dict=False)

    ids, lens, paths = dataset.load()

    # dataset.pre_compute_features(paths)

    # Print a small sample from the dataset.
    # for p, s, l in zip(paths[:10], ids[:10], lens[:10]):
    #     print(p, np.fromstring(s, dtype=np.int32)[:10], l)

    # Collect and print the decibel statistics for all the files.
    # print("Collecting decibel statistics for {} files ...".format(len(paths)))
    # min_linear_db, max_linear_db, min_mel_db, max_mel_db = collect_decibel_statistics(paths)
    # print("avg. min. linear magnitude (dB)", min_linear_db)  # -99.94
    # print("avg. max. linear magnitude (dB)", max_linear_db)  # 36.50
    # print("avg. min. mel magnitude (dB)", min_mel_db)        # -92.22
    # print("avg. max. mel magnitude (dB)", max_mel_db)        # 9.33
