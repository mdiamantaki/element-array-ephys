import gc
import importlib
import inspect
import pathlib
import re
from decimal import Decimal

import datajoint as dj
import numpy as np
import pandas as pd
from element_interface.utils import dict_to_uuid, find_full_path, find_root_directory

from . import probe
from .readers import kilosort, openephys, spikeglx

logger = dj.logger

schema = dj.schema()

_linking_module = None


def activate(
    ephys_schema_name: str,
    *,
    create_schema: bool = True,
    create_tables: bool = True,
    linking_module: str = None,
):
    """Activates the `ephys` and `probe` schemas.

    Args:
        ephys_schema_name (str): A string containing the name of the ephys schema.
        create_schema (bool): If True, schema will be created in the database.
        create_tables (bool): If True, tables related to the schema will be created in the database.
        linking_module (str): A string containing the module name or module containing the required dependencies to activate the schema.

    Dependencies:
    Upstream tables:
        Session: A parent table to ProbeInsertion
        Probe: A parent table to EphysRecording. Probe information is required before electrophysiology data is imported.

    Functions:
        get_ephys_root_data_dir(): Returns absolute path for root data director(y/ies) with all electrophysiological recording sessions, as a list of string(s).
        get_session_direction(session_key: dict): Returns path to electrophysiology data for the a particular session as a list of strings.
        get_processed_data_dir(): Optional. Returns absolute path for processed data. Defaults to root directory.
    """

    if isinstance(linking_module, str):
        linking_module = importlib.import_module(linking_module)
    assert inspect.ismodule(
        linking_module
    ), "The argument 'dependency' must be a module's name or a module"

    global _linking_module
    _linking_module = linking_module

    if not probe.schema.is_activated():
        raise RuntimeError("Please activate the `probe` schema first.")

    schema.activate(
        ephys_schema_name,
        create_schema=create_schema,
        create_tables=create_tables,
        add_objects=_linking_module.__dict__,
    )


# -------------- Functions required by the elements-ephys  ---------------

def get_ephys_root_data_dir():
    """Retrieve ephys root data directory."""
    ephys_root_dirs = dj.config.get("custom", {}).get("ephys_root_data_dir", None)
    if not ephys_root_dirs:
        return None
    elif isinstance(ephys_root_dirs, (str, pathlib.Path)):
        return [ephys_root_dirs]
    elif isinstance(ephys_root_dirs, list):
        return ephys_root_dirs
    else:
        raise TypeError("`ephys_root_data_dir` must be a string, pathlib, or list")
        
# def get_ephys_root_data_dir() -> list:
#     """Fetches absolute data path to ephys data directories.

#     The absolute path here is used as a reference for all downstream relative paths used in DataJoint.

#     Returns:
#         A list of the absolute path(s) to ephys data directories.
#     """
#     root_directories = _linking_module.get_ephys_root_data_dir()
#     if isinstance(root_directories, (str, pathlib.Path)):
#         root_directories = [root_directories]

#     if hasattr(_linking_module, "get_processed_root_data_dir"):
#         root_directories.append(_linking_module.get_processed_root_data_dir())

#     return _linking_module.get_ephys_root_data_dir(session_key)


def get_session_directory(session_key: dict) -> str:
    """Retrieve the session directory with Neuropixels for the given session.

    Args:
        session_key (dict): A dictionary mapping subject to an entry in the subject table, and session_datetime corresponding to a session in the database.

    Returns:
        A string for the path to the session directory.
    """
    return _linking_module.get_session_directory(session_key)


def get_processed_root_data_dir() -> str:
    """Retrieve the root directory for all processed data.

    Returns:
        A string for the full path to the root directory for processed data.
    """

    if hasattr(_linking_module, "get_processed_root_data_dir"):
        return _linking_module.get_processed_root_data_dir()
    else:
        return get_ephys_root_data_dir()[0]


# ----------------------------- Table declarations ----------------------


@schema
class AcquisitionSoftware(dj.Lookup):
    """Name of software used for recording electrophysiological data.

    Attributes:
        acq_software ( varchar(24) ): Acquisition software, e.g,. SpikeGLX, OpenEphys
    """

    definition = """  # Name of software used for recording of neuropixels probes - SpikeGLX or Open Ephys
    acq_software: varchar(24)    
    """
    contents = zip(["SpikeGLX", "Open Ephys"])


@schema
class ProbeInsertion(dj.Manual):
    """Information about probe insertion across subjects and sessions.

    Attributes:
        Session (foreign key): Session primary key.
        insertion_number (foreign key, str): Unique insertion number for each probe insertion for a given session.
        probe.Probe (str): probe.Probe primary key.
    """

    definition = """
    # Probe insertion implanted into an animal for a given session.
    -> Session
    insertion_number: tinyint unsigned
    ---
    -> probe.Probe
    """

    @classmethod
    def auto_generate_entries(cls, session_key):
        """Automatically populate entries in ProbeInsertion table for a session."""
        session_dir = find_full_path(
            get_ephys_root_data_dir(), get_session_directory(session_key)
        )
        # search session dir and determine acquisition software
        for ephys_pattern, ephys_acq_type in (
            ("*.ap.meta", "SpikeGLX"),
            ("*.oebin", "Open Ephys"),
        ):
            ephys_meta_filepaths = list(session_dir.rglob(ephys_pattern))
            if ephys_meta_filepaths:
                acq_software = ephys_acq_type
                break
        else:
            raise FileNotFoundError(
                f"Ephys recording data not found!"
                f" Neither SpikeGLX nor Open Ephys recording files found in: {session_dir}"
            )

        probe_list, probe_insertion_list = [], []
        if acq_software == "SpikeGLX":
            for meta_fp_idx, meta_filepath in enumerate(ephys_meta_filepaths):
                spikeglx_meta = spikeglx.SpikeGLXMeta(meta_filepath)

                probe_key = {
                    "probe_type": spikeglx_meta.probe_model,
                    "probe": spikeglx_meta.probe_SN,
                }
                if probe_key["probe"] not in [p["probe"] for p in probe_list]:
                    probe_list.append(probe_key)

                probe_dir = meta_filepath.parent
                try:
                    probe_number = re.search(r"(imec)?\d{1}$", probe_dir.name).group()
                    probe_number = int(probe_number.replace("imec", ""))
                except AttributeError:
                    probe_number = meta_fp_idx

                probe_insertion_list.append(
                    {
                        **session_key,
                        "probe": spikeglx_meta.probe_SN,
                        "insertion_number": int(probe_number),
                    }
                )
        elif acq_software == "Open Ephys":
            loaded_oe = openephys.OpenEphys(session_dir)
            for probe_idx, oe_probe in enumerate(loaded_oe.probes.values()):
                probe_key = {
                    "probe_type": oe_probe.probe_model,
                    "probe": oe_probe.probe_SN,
                }
                if probe_key["probe"] not in [p["probe"] for p in probe_list]:
                    probe_list.append(probe_key)
                probe_insertion_list.append(
                    {
                        **session_key,
                        "probe": oe_probe.probe_SN,
                        "insertion_number": probe_idx,
                    }
                )
        else:
            raise NotImplementedError(f"Unknown acquisition software: {acq_software}")

        probe.Probe.insert(probe_list, skip_duplicates=True)
        cls.insert(probe_insertion_list, skip_duplicates=True)


@schema
class InsertionLocation(dj.Manual):
    """Stereotaxic location information for each probe insertion.

    Attributes:
        ProbeInsertion (foreign key): ProbeInsertion primary key.
        SkullReference (dict): SkullReference primary key.
        ap_location (decimal (6, 2) ): Anterior-posterior location in micrometers. Reference is 0 with anterior values positive.
        ml_location (decimal (6, 2) ): Medial-lateral location in micrometers. Reference is zero with right side values positive.
        depth (decimal (6, 2) ): Manipulator depth relative to the surface of the brain at zero. Ventral is negative.
        Theta (decimal (5, 2) ): elevation - rotation about the ml-axis in degrees relative to positive z-axis.
        phi (decimal (5, 2) ): azimuth - rotation about the dv-axis in degrees relative to the positive x-axis

    """

    definition = """
    # Brain Location of a given probe insertion.
    -> ProbeInsertion
    ---
    -> SkullReference
    ap_location: decimal(6, 2) # (um) anterior-posterior; ref is 0; more anterior is more positive
    ml_location: decimal(6, 2) # (um) medial axis; ref is 0 ; more right is more positive
    depth:       decimal(6, 2) # (um) manipulator depth relative to surface of the brain (0); more ventral is more negative
    theta=null:  decimal(5, 2) # (deg) - elevation - rotation about the ml-axis [0, 180] - w.r.t the z+ axis
    phi=null:    decimal(5, 2) # (deg) - azimuth - rotation about the dv-axis [0, 360] - w.r.t the x+ axis
    beta=null:   decimal(5, 2) # (deg) rotation about the shank of the probe [-180, 180] - clockwise is increasing in degree - 0 is the probe-front facing anterior
    """


@schema
class EphysRecording(dj.Imported):
    """Automated table with electrophysiology recording information for each probe inserted during an experimental session.

    Attributes:
        ProbeInsertion (foreign key): ProbeInsertion primary key.
        probe.ElectrodeConfig (dict): probe.ElectrodeConfig primary key.
        AcquisitionSoftware (dict): AcquisitionSoftware primary key.
        sampling_rate (float): sampling rate of the recording in Hertz (Hz).
        recording_datetime (datetime): datetime of the recording from this probe.
        recording_duration (float): duration of the entire recording from this probe in seconds.
    """

    definition = """
    # Ephys recording from a probe insertion for a given session.
    -> ProbeInsertion      
    ---
    -> probe.ElectrodeConfig
    -> AcquisitionSoftware
    sampling_rate: float # (Hz) 
    recording_datetime: datetime # datetime of the recording from this probe
    recording_duration: float # (seconds) duration of the recording from this probe
    """

    class Channel(dj.Part):
        definition = """
        -> master
        channel_idx: int  # channel index (index of the raw data)
        ---
        -> probe.ElectrodeConfig.Electrode
        channel_name="": varchar(64)  # alias of the channel
        """

    class EphysFile(dj.Part):
        """Paths of electrophysiology recording files for each insertion.

        Attributes:
            EphysRecording (foreign key): EphysRecording primary key.
            file_path (varchar(255) ): relative file path for electrophysiology recording.
        """

        definition = """
        # Paths of files of a given EphysRecording round.
        -> master
        file_path: varchar(255)  # filepath relative to root data directory
        """

    def make(self, key):
        """Populates table with electrophysiology recording information."""
        session_dir = find_full_path(
            get_ephys_root_data_dir(), get_session_directory(key)
        )
        inserted_probe_serial_number = (ProbeInsertion * probe.Probe & key).fetch1(
            "probe"
        )

        # Search session dir and determine acquisition software
        for ephys_pattern, ephys_acq_type in (
            ("*.ap.meta", "SpikeGLX"),
            ("*.oebin", "Open Ephys"),
        ):
            ephys_meta_filepaths = list(session_dir.rglob(ephys_pattern))
            if ephys_meta_filepaths:
                acq_software = ephys_acq_type
                break
        else:
            raise FileNotFoundError(
                f"Ephys recording data not found in {session_dir}."
                "Neither SpikeGLX nor Open Ephys recording files found"
            )

        if acq_software not in AcquisitionSoftware.fetch("acq_software"):
            raise NotImplementedError(
                f"Processing ephys files from acquisition software of type {acq_software} is not yet implemented."
            )

        supported_probe_types = probe.ProbeType.fetch("probe_type")

        if acq_software == "SpikeGLX":
            for meta_filepath in ephys_meta_filepaths:
                spikeglx_meta = spikeglx.SpikeGLXMeta(meta_filepath)
                if str(spikeglx_meta.probe_SN) == inserted_probe_serial_number:
                    spikeglx_meta_filepath = meta_filepath
                    break
            else:
                raise FileNotFoundError(
                    "No SpikeGLX data found for probe insertion: {}".format(key)
                )

            if spikeglx_meta.probe_model not in supported_probe_types:
                raise NotImplementedError(
                    f"Processing for neuropixels probe model {spikeglx_meta.probe_model} not yet implemented."
                )

            probe_type = spikeglx_meta.probe_model
            electrode_query = probe.ProbeType.Electrode & {"probe_type": probe_type}

            probe_electrodes = {
                (shank, shank_col, shank_row): key
                for key, shank, shank_col, shank_row in zip(
                    *electrode_query.fetch("KEY", "shank", "shank_col", "shank_row")
                )
            }  # electrode configuration
            electrode_group_members = [
                probe_electrodes[(shank, shank_col, shank_row)]
                for shank, shank_col, shank_row, _ in spikeglx_meta.shankmap["data"]
            ]  # recording session-specific electrode configuration

            econfig_entry, econfig_electrodes = generate_electrode_config_entry(
                probe_type, electrode_group_members
            )

            ephys_recording_entry = {
                **key,
                "electrode_config_hash": econfig_entry["electrode_config_hash"],
                "acq_software": acq_software,
                "sampling_rate": spikeglx_meta.meta["imSampRate"],
                "recording_datetime": spikeglx_meta.recording_time,
                "recording_duration": (
                    spikeglx_meta.recording_duration
                    or spikeglx.retrieve_recording_duration(spikeglx_meta_filepath)
                ),
            }

            root_dir = find_root_directory(
                get_ephys_root_data_dir(), spikeglx_meta_filepath
            )

            ephys_file_entries = [
                {
                    **key,
                    "file_path": spikeglx_meta_filepath.relative_to(
                        root_dir
                    ).as_posix(),
                }
            ]

            # Insert channel information
            # Get channel and electrode-site mapping
            channel2electrode_map = {
                recorded_site: probe_electrodes[(shank, shank_col, shank_row)]
                for recorded_site, (shank, shank_col, shank_row, _) in enumerate(
                    spikeglx_meta.shankmap["data"]
                )
            }

            ephys_channel_entries = [
                {
                    **key,
                    "electrode_config_hash": econfig_entry["electrode_config_hash"],
                    "channel_idx": channel_idx,
                    **channel_info,
                }
                for channel_idx, channel_info in channel2electrode_map.items()
            ]
        elif acq_software == "Open Ephys":
            #print(session_dir)
            dataset = openephys.OpenEphys(session_dir)
            #print(dataset.probes.items())
            for serial_number, probe_data in dataset.probes.items():
                if str(serial_number) == inserted_probe_serial_number:
                    break
            else:
                raise FileNotFoundError(
                    "No Open Ephys data found for probe insertion: {}".format(key)
                )
            #print(probe_data)
            #print(probe_data.ap_meta)
            if not probe_data.ap_meta:
                raise IOError(
                    'No analog signals found - check "structure.oebin" file or "continuous" directory'
                )

            if probe_data.probe_model not in supported_probe_types:
                raise NotImplementedError(
                    f"Processing for neuropixels probe model {probe_data.probe_model} not yet implemented."
                )

            probe_type = probe_data.probe_model
            electrode_query = probe.ProbeType.Electrode & {"probe_type": probe_type}

            probe_electrodes = {
                key["electrode"]: key for key in electrode_query.fetch("KEY")
            }  # electrode configuration
            
            # it gives channel_indices starting from -1, it's obviously wrong!
            electrode_group_members = [
                probe_electrodes[channel_idx+1]
                for channel_idx in probe_data.ap_meta["channels_indices"]
            ]  # recording session-specific electrode configuration

            econfig_entry, econfig_electrodes = generate_electrode_config_entry(
                probe_type, electrode_group_members
            )

            ephys_recording_entry = {
                **key,
                "electrode_config_hash": econfig_entry["electrode_config_hash"],
                "acq_software": acq_software,
                "sampling_rate": probe_data.ap_meta["sample_rate"],
                "recording_datetime": probe_data.recording_info["recording_datetimes"][
                    0
                ],
                "recording_duration": np.sum(
                    probe_data.recording_info["recording_durations"]
                ),
            }
            # print('line 469')
            # print(probe_data.recording_info["recording_files"][0])
            root_dir = find_root_directory(
                get_ephys_root_data_dir(),
                probe_data.recording_info["recording_files"][0],
            )

            ephys_file_entries = [
                {**key, "file_path": fp.relative_to(root_dir).as_posix()}
                for fp in probe_data.recording_info["recording_files"]
            ]

            channel2electrode_map = {
                channel_idx: probe_electrodes[channel_idx+1]
                for channel_idx in probe_data.ap_meta["channels_indices"]
            }

            ephys_channel_entries = [
                {
                    **key,
                    "electrode_config_hash": econfig_entry["electrode_config_hash"],
                    "channel_idx": channel_idx,
                    **channel_info,
                }
                for channel_idx, channel_info in channel2electrode_map.items()
            ]

            # Explicitly garbage collect "dataset" as these may have large memory footprint and may not be cleared fast enough
            del probe_data, dataset
            gc.collect()
        else:
            raise NotImplementedError(
                f"Processing ephys files from acquisition software of type {acq_software} is not yet implemented."
            )

        # Insert into probe.ElectrodeConfig (recording configuration)
        if not probe.ElectrodeConfig & {
            "electrode_config_hash": econfig_entry["electrode_config_hash"]
        }:
            probe.ElectrodeConfig.insert1(econfig_entry)
            probe.ElectrodeConfig.Electrode.insert(econfig_electrodes)

        self.insert1(ephys_recording_entry)
        self.EphysFile.insert(ephys_file_entries)
        self.Channel.insert(ephys_channel_entries)


@schema
class LFP(dj.Imported):
    """Extracts local field potentials (LFP) from an electrophysiology recording.

    Attributes:
        EphysRecording (foreign key): EphysRecording primary key.
        lfp_sampling_rate (float): Sampling rate for LFPs in Hz.
        lfp_time_stamps (longblob): Time stamps with respect to the start of the recording.
        lfp_mean (longblob): Overall mean LFP across electrodes.
    """

    definition = """
    # Acquired local field potential (LFP) from a given Ephys recording.
    -> EphysRecording
    ---
    lfp_sampling_rate: float   # (Hz)
    lfp_time_stamps: longblob  # (s) timestamps with respect to the start of the recording (recording_timestamp)
    lfp_mean: longblob         # (uV) mean of LFP across electrodes - shape (time,)
    """

    class Electrode(dj.Part):
        """Saves local field potential data for each electrode.

        Attributes:
            LFP (foreign key): LFP primary key.
            probe.ElectrodeConfig.Electrode (foreign key): probe.ElectrodeConfig.Electrode primary key.
            lfp (longblob): LFP recording at this electrode in microvolts.
        """

        definition = """
        -> master
        -> probe.ElectrodeConfig.Electrode  
        ---
        lfp: longblob               # (uV) recorded lfp at this electrode 
        """

    # Only store LFP for every 9th channel, due to high channel density,
    # close-by channels exhibit highly similar LFP
    _skip_channel_counts = 9

    def make(self, key):
        """Populates the LFP tables."""
        acq_software = (EphysRecording * ProbeInsertion & key).fetch1("acq_software")

        electrode_keys, lfp = [], []

        if acq_software == "SpikeGLX":
            spikeglx_meta_filepath = get_spikeglx_meta_filepath(key)
            spikeglx_recording = spikeglx.SpikeGLX(spikeglx_meta_filepath.parent)

            lfp_channel_ind = spikeglx_recording.lfmeta.recording_channels[
                -1 :: -self._skip_channel_counts
            ]

            # Extract LFP data at specified channels and convert to uV
            lfp = spikeglx_recording.lf_timeseries[
                :, lfp_channel_ind
            ]  # (sample x channel)
            lfp = (
                lfp * spikeglx_recording.get_channel_bit_volts("lf")[lfp_channel_ind]
            ).T  # (channel x sample)

            self.insert1(
                dict(
                    key,
                    lfp_sampling_rate=spikeglx_recording.lfmeta.meta["imSampRate"],
                    lfp_time_stamps=(
                        np.arange(lfp.shape[1])
                        / spikeglx_recording.lfmeta.meta["imSampRate"]
                    ),
                    lfp_mean=lfp.mean(axis=0),
                )
            )

            electrode_query = (
                probe.ProbeType.Electrode
                * probe.ElectrodeConfig.Electrode
                * EphysRecording
                & key
            )
            probe_electrodes = {
                (shank, shank_col, shank_row): key
                for key, shank, shank_col, shank_row in zip(
                    *electrode_query.fetch("KEY", "shank", "shank_col", "shank_row")
                )
            }

            for recorded_site in lfp_channel_ind:
                shank, shank_col, shank_row, _ = spikeglx_recording.apmeta.shankmap[
                    "data"
                ][recorded_site]
                electrode_keys.append(probe_electrodes[(shank, shank_col, shank_row)])
        elif acq_software == "Open Ephys":
            oe_probe = get_openephys_probe_data(key)
            print(oe_probe)
            print(oe_probe.lfp_meta)
            lfp_channel_ind = np.r_[
                len(oe_probe.lfp_meta["channels_indices"])
                - 1 : 0 : -self._skip_channel_counts
            ]

            lfp = oe_probe.lfp_timeseries[:, lfp_channel_ind]  # (sample x channel)
            lfp = (
                lfp * np.array(oe_probe.lfp_meta["channels_gains"])[lfp_channel_ind]
            ).T  # (channel x sample)
            lfp_timestamps = oe_probe.lfp_timestamps

            self.insert1(
                dict(
                    key,
                    lfp_sampling_rate=oe_probe.lfp_meta["sample_rate"],
                    lfp_time_stamps=lfp_timestamps,
                    lfp_mean=lfp.mean(axis=0),
                )
            )

            electrode_query = (
                probe.ProbeType.Electrode
                * probe.ElectrodeConfig.Electrode
                * EphysRecording
                & key
            )
            probe_electrodes = {
                key["electrode"]: key for key in electrode_query.fetch("KEY")
            }

            electrode_keys.extend(
                probe_electrodes[channel_idx] for channel_idx in lfp_channel_ind
            )
        else:
            raise NotImplementedError(
                f"LFP extraction from acquisition software"
                f" of type {acq_software} is not yet implemented"
            )

        # single insert in loop to mitigate potential memory issue
        for electrode_key, lfp_trace in zip(electrode_keys, lfp):
            self.Electrode.insert1({**key, **electrode_key, "lfp": lfp_trace})


# ------------ Clustering --------------


@schema
class ClusteringMethod(dj.Lookup):
    """Kilosort clustering method.

    Attributes:
        clustering_method (foreign key, varchar(16) ): Kilosort clustering method.
        clustering_methods_desc (varchar(1000) ): Additional description of the clustering method.
    """

    definition = """
    # Method for clustering
    clustering_method: varchar(16)
    ---
    clustering_method_desc: varchar(1000)
    """

    contents = [
        ("kilosort2", "kilosort2 clustering method"),
        ("kilosort2.5", "kilosort2.5 clustering method"),
        ("kilosort3", "kilosort3 clustering method"),
    ]


@schema
class ClusteringParamSet(dj.Lookup):
    """Parameters to be used in clustering procedure for spike sorting.

    Attributes:
        paramset_idx (foreign key): Unique ID for the clustering parameter set.
        ClusteringMethod (dict): ClusteringMethod primary key.
        paramset_desc (varchar(128) ): Description of the clustering parameter set.
        param_set_hash (uuid): UUID hash for the parameter set.
        params (longblob): Set of clustering parameters.
    """

    definition = """
    # Parameter set to be used in a clustering procedure
    paramset_idx:  smallint
    ---
    -> ClusteringMethod    
    paramset_desc: varchar(128)
    param_set_hash: uuid
    unique index (param_set_hash)
    params: longblob  # dictionary of all applicable parameters
    """

    @classmethod
    def insert_new_params(
        cls,
        clustering_method: str,
        paramset_desc: str,
        params: dict,
        paramset_idx: int = None,
    ):
        """Inserts new parameters into the ClusteringParamSet table.

        Args:
            clustering_method (str): name of the clustering method.
            paramset_desc (str): description of the parameter set
            params (dict): clustering parameters
            paramset_idx (int, optional): Unique parameter set ID. Defaults to None.
        """
        if paramset_idx is None:
            paramset_idx = (
                dj.U().aggr(cls, n="max(paramset_idx)").fetch1("n") or 0
            ) + 1

        param_dict = {
            "clustering_method": clustering_method,
            "paramset_idx": paramset_idx,
            "paramset_desc": paramset_desc,
            "params": params,
            "param_set_hash": dict_to_uuid(
                {**params, "clustering_method": clustering_method}
            ),
        }
        param_query = cls & {"param_set_hash": param_dict["param_set_hash"]}

        if param_query:  # If the specified param-set already exists
            existing_paramset_idx = param_query.fetch1("paramset_idx")
            if (
                existing_paramset_idx == paramset_idx
            ):  # If the existing set has the same paramset_idx: job done
                return
            else:  # If not same name: human error, trying to add the same paramset with different name
                raise dj.DataJointError(
                    f"The specified param-set already exists"
                    f" - with paramset_idx: {existing_paramset_idx}"
                )
        else:
            if {"paramset_idx": paramset_idx} in cls.proj():
                raise dj.DataJointError(
                    f"The specified paramset_idx {paramset_idx} already exists,"
                    f" please pick a different one."
                )
            cls.insert1(param_dict)


@schema
class ClusterQualityLabel(dj.Lookup):
    """Quality label for each spike sorted cluster.

    Attributes:
        cluster_quality_label (foreign key, varchar(100) ): Cluster quality type.
        cluster_quality_description (varchar(4000) ): Description of the cluster quality type.
    """

    definition = """
    # Quality
    cluster_quality_label:  varchar(100)  # cluster quality type - e.g. 'good', 'MUA', 'noise', etc.
    ---
    cluster_quality_description:  varchar(4000)
    """
    contents = [
        ("good", "single unit"),
        ("ok", "probably a single unit, but could be contaminated"),
        ("mua", "multi-unit activity"),
        ("noise", "bad unit"),
        ("n.a.", "not available"),
    ]


@schema
class ClusteringTask(dj.Manual):
    """A clustering task to spike sort electrophysiology datasets.

    Attributes:
        EphysRecording (foreign key): EphysRecording primary key.
        ClusteringParamSet (foreign key): ClusteringParamSet primary key.
        clustering_outdir_dir (varchar (255) ): Relative path to output clustering results.
        task_mode (enum): `Trigger` computes clustering or and `load` imports existing data.
    """

    definition = """
    # Manual table for defining a clustering task ready to be run
    -> EphysRecording
    -> ClusteringParamSet
    ---
    clustering_output_dir='': varchar(255)  #  clustering output directory relative to the clustering root data directory
    task_mode='load': enum('load', 'trigger')  # 'load': load computed analysis results, 'trigger': trigger computation
    """

    @classmethod
    def infer_output_dir(cls, key, relative: bool = False, mkdir: bool = False):
        """Infer output directory if it is not provided.

        Args:
            key (dict): ClusteringTask primary key.

        Returns:
            Pathlib.Path: Expected clustering_output_dir based on the following convention: processed_dir / session_dir / probe_{insertion_number} / {clustering_method}_{paramset_idx}
            e.g.: sub4/sess1/probe_2/kilosort2_0
        """
        processed_dir = pathlib.Path(get_processed_root_data_dir())
        session_dir = find_full_path(
            get_ephys_root_data_dir(), get_session_directory(key)
        )
        root_dir = find_root_directory(get_ephys_root_data_dir(), session_dir)

        method = (
            (ClusteringParamSet * ClusteringMethod & key)
            .fetch1("clustering_method")
            .replace(".", "-")
        )

        output_dir = (
            processed_dir
            / session_dir.relative_to(root_dir)
            / f'probe_{key["insertion_number"]}'
            / f'{method}_{key["paramset_idx"]}'
        )

        if mkdir:
            output_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"{output_dir} created!")

        return output_dir.relative_to(processed_dir) if relative else output_dir

    @classmethod
    def auto_generate_entries(cls, ephys_recording_key: dict, paramset_idx: int = 0):
        """Autogenerate entries based on a particular ephys recording.

        Args:
            ephys_recording_key (dict): EphysRecording primary key.
            paramset_idx (int, optional): Parameter index to use for clustering task. Defaults to 0.
        """
        key = {**ephys_recording_key, "paramset_idx": paramset_idx}

        processed_dir = get_processed_root_data_dir()
        output_dir = ClusteringTask.infer_output_dir(key, relative=False, mkdir=True)

        try:
            kilosort.Kilosort(
                output_dir
            )  # check if the directory is a valid Kilosort output
        except FileNotFoundError:
            task_mode = "trigger"
        else:
            task_mode = "load"

        cls.insert1(
            {
                **key,
                "clustering_output_dir": output_dir.relative_to(
                    processed_dir
                ).as_posix(),
                "task_mode": task_mode,
            }
        )


@schema
class Clustering(dj.Imported):
    """A processing table to handle each clustering task.

    Attributes:
        ClusteringTask (foreign key): ClusteringTask primary key.
        clustering_time (datetime): Time when clustering results are generated.
        package_version (varchar(16) ): Package version used for a clustering analysis.
    """

    definition = """
    # Clustering Procedure
    -> ClusteringTask
    ---
    clustering_time: datetime  # time of generation of this set of clustering results 
    package_version='': varchar(16)
    """

    def make(self, key):
        """Triggers or imports clustering analysis."""
        task_mode, output_dir = (ClusteringTask & key).fetch1(
            "task_mode", "clustering_output_dir"
        )

        if not output_dir:
            output_dir = ClusteringTask.infer_output_dir(key, relative=True, mkdir=True)
            # update clustering_output_dir
            ClusteringTask.update1(
                {**key, "clustering_output_dir": output_dir.as_posix()}
            )

        kilosort_dir = find_full_path(get_ephys_root_data_dir(), output_dir)

        if task_mode == "load":
            kilosort.Kilosort(
                kilosort_dir
            )  # check if the directory is a valid Kilosort output
        elif task_mode == "trigger":
            acq_software, clustering_method, params = (
                ClusteringTask * EphysRecording * ClusteringParamSet & key
            ).fetch1("acq_software", "clustering_method", "params")

            if "kilosort" in clustering_method:
                from .spike_sorting import kilosort_triggering

                # add additional probe-recording and channels details into `params`
                params = {**params, **get_recording_channels_details(key)}
                params["fs"] = params["sample_rate"]

                if acq_software == "SpikeGLX":
                    spikeglx_meta_filepath = get_spikeglx_meta_filepath(key)
                    spikeglx_recording = spikeglx.SpikeGLX(
                        spikeglx_meta_filepath.parent
                    )
                    spikeglx_recording.validate_file("ap")

                    if clustering_method.startswith("pykilosort"):
                        kilosort_triggering.run_pykilosort(
                            continuous_file=spikeglx_recording.root_dir
                            / (spikeglx_recording.root_name + ".ap.bin"),
                            kilosort_output_directory=kilosort_dir,
                            channel_ind=params.pop("channel_ind"),
                            x_coords=params.pop("x_coords"),
                            y_coords=params.pop("y_coords"),
                            shank_ind=params.pop("shank_ind"),
                            connected=params.pop("connected"),
                            sample_rate=params.pop("sample_rate"),
                            params=params,
                        )
                    else:
                        run_kilosort = kilosort_triggering.SGLXKilosortPipeline(
                            npx_input_dir=spikeglx_meta_filepath.parent,
                            ks_output_dir=kilosort_dir,
                            params=params,
                            KS2ver=f'{Decimal(clustering_method.replace("kilosort", "")):.1f}',
                            run_CatGT=True,
                        )
                        run_kilosort.run_modules()
                elif acq_software == "Open Ephys":
                    oe_probe = get_openephys_probe_data(key)

                    assert len(oe_probe.recording_info["recording_files"]) == 1

                    # run kilosort
                    if clustering_method.startswith("pykilosort"):
                        kilosort_triggering.run_pykilosort(
                            continuous_file=pathlib.Path(
                                oe_probe.recording_info["recording_files"][0]
                            )
                            / "continuous.dat",
                            kilosort_output_directory=kilosort_dir,
                            channel_ind=params.pop("channel_ind"),
                            x_coords=params.pop("x_coords"),
                            y_coords=params.pop("y_coords"),
                            shank_ind=params.pop("shank_ind"),
                            connected=params.pop("connected"),
                            sample_rate=params.pop("sample_rate"),
                            params=params,
                        )
                    else:
                        run_kilosort = kilosort_triggering.OpenEphysKilosortPipeline(
                            npx_input_dir=oe_probe.recording_info["recording_files"][0],
                            ks_output_dir=kilosort_dir,
                            params=params,
                            KS2ver=f'{Decimal(clustering_method.replace("kilosort", "")):.1f}',
                        )
                        run_kilosort.run_modules()
            else:
                raise NotImplementedError(
                    f"Automatic triggering of {clustering_method}"
                    f" clustering analysis is not yet supported"
                )

        else:
            raise ValueError(f"Unknown task mode: {task_mode}")

        creation_time, _, _ = kilosort.extract_clustering_info(kilosort_dir)
        self.insert1({**key, "clustering_time": creation_time, "package_version": ""})


@schema
class CuratedClustering(dj.Imported):
    """Clustering results after curation.

    Attributes:
        Clustering (foreign key): Clustering primary key.
    """

    definition = """
    # Clustering results of the spike sorting step.
    -> Clustering    
    """

    class Unit(dj.Part):
        """Single unit properties after clustering and curation.

        Attributes:
            CuratedClustering (foreign key): CuratedClustering primary key.
            unit (int): Unique integer identifying a single unit.
            probe.ElectrodeConfig.Electrode (foreign key): probe.ElectrodeConfig.Electrode primary key.
            ClusteringQualityLabel (foreign key): CLusteringQualityLabel primary key.
            spike_count (int): Number of spikes in this recording for this unit.
            spike_times (longblob): Spike times of this unit, relative to start time of EphysRecording.
            spike_sites (longblob): Array of electrode associated with each spike.
            spike_depths (longblob): Array of depths associated with each spike, relative to each spike.
        """

        definition = """   
        # Properties of a given unit from a round of clustering (and curation)
        -> master
        unit: int
        ---
        -> probe.ElectrodeConfig.Electrode  # electrode with highest waveform amplitude for this unit
        -> ClusterQualityLabel
        spike_count: int         # how many spikes in this recording for this unit
        spike_times: longblob    # (s) spike times of this unit, relative to the start of the EphysRecording
        spike_sites : longblob   # array of electrode associated with each spike
        spike_depths=null : longblob  # (um) array of depths associated with each spike, relative to the (0, 0) of the probe    
        """

    def make(self, key):
        """Automated population of Unit information."""
        clustering_method, output_dir = (
            ClusteringTask * ClusteringParamSet & key
        ).fetch1("clustering_method", "clustering_output_dir")
        output_dir = find_full_path(get_ephys_root_data_dir(), output_dir)

        # Get channel and electrode-site mapping
        electrode_query = (EphysRecording.Channel & key).proj(..., "-channel_name")
        #print('line1036')
        #print(electrode_query.fetch(as_dict=True))
        channel2electrode_map: dict[int, dict] = {
            chn.pop("channel_idx")+1: chn for chn in electrode_query.fetch(as_dict=True)
        }

        # Get sorter method and create output directory.
        sorter_name = clustering_method.replace(".", "_")
        si_sorting_analyzer_dir = output_dir / sorter_name / "sorting_analyzer"

        if si_sorting_analyzer_dir.exists():  # Read from spikeinterface outputs
            import spikeinterface as si
            from spikeinterface import sorters

            sorting_file = output_dir / sorter_name / "spike_sorting" / "si_sorting.pkl"
            si_sorting_: si.sorters.BaseSorter = si.load_extractor(
                sorting_file, base_folder=output_dir
            )
            if si_sorting_.unit_ids.size == 0:
                logger.info(
                    f"No units found in {sorting_file}. Skipping Unit ingestion..."
                )
                self.insert1(key)
                return

            sorting_analyzer = si.load_sorting_analyzer(folder=si_sorting_analyzer_dir)
            si_sorting = sorting_analyzer.sorting

            # Find representative channel for each unit
            unit_peak_channel: dict[int, np.ndarray] = (
                si.ChannelSparsity.from_best_channels(
                    sorting_analyzer,
                    1,
                ).unit_id_to_channel_indices
            )
            unit_peak_channel: dict[int, int] = {
                u: chn[0] for u, chn in unit_peak_channel.items()
            }

            spike_count_dict: dict[int, int] = si_sorting.count_num_spikes_per_unit()
            # {unit: spike_count}

            # update channel2electrode_map to match with probe's channel index
            channel2electrode_map = {
                idx: channel2electrode_map[int(chn_idx)]
                for idx, chn_idx in enumerate(sorting_analyzer.get_probe().contact_ids)
            }

            # Get unit id to quality label mapping
            cluster_quality_label_map = {
                int(unit_id): (
                    si_sorting.get_unit_property(unit_id, "KSLabel")
                    if "KSLabel" in si_sorting.get_property_keys()
                    else "n.a."
                )
                for unit_id in si_sorting.unit_ids
            }

            spike_locations = sorting_analyzer.get_extension("spike_locations")
            extremum_channel_inds = si.template_tools.get_template_extremum_channel(
                sorting_analyzer, outputs="index"
            )
            spikes_df = pd.DataFrame(
                sorting_analyzer.sorting.to_spike_vector(
                    extremum_channel_inds=extremum_channel_inds
                )
            )

            units = []
            for unit_idx, unit_id in enumerate(si_sorting.unit_ids):
                unit_id = int(unit_id)
                unit_spikes_df = spikes_df[spikes_df.unit_index == unit_idx]
                spike_sites = np.array(
                    [
                        channel2electrode_map[chn_idx]["electrode"]
                        for chn_idx in unit_spikes_df.channel_index
                    ]
                )
                unit_spikes_loc = spike_locations.get_data()[unit_spikes_df.index]
                _, spike_depths = zip(*unit_spikes_loc)  # x-coordinates, y-coordinates
                spike_times = si_sorting.get_unit_spike_train(
                    unit_id, return_times=True
                )

                assert len(spike_times) == len(spike_sites) == len(spike_depths)

                units.append(
                    {
                        **key,
                        **channel2electrode_map[unit_peak_channel[unit_id]],
                        "unit": unit_id,
                        "cluster_quality_label": cluster_quality_label_map[unit_id],
                        "spike_times": spike_times,
                        "spike_count": spike_count_dict[unit_id],
                        "spike_sites": spike_sites,
                        "spike_depths": spike_depths,
                    }
                )
        else:  # read from kilosort outputs
            kilosort_dataset = kilosort.Kilosort(output_dir)
            print(kilosort_dataset)
            acq_software, sample_rate = (EphysRecording & key).fetch1(
                "acq_software", "sampling_rate"
            )

            sample_rate = kilosort_dataset.data["params"].get(
                "sample_rate", sample_rate
            )

            # ---------- Unit ----------
            # # -- Remove 0-spike units
            print(len(kilosort_dataset.data["cluster_ids"]))
            print(len(kilosort_dataset.data["spike_clusters"]))
            # withspike_idx = [
            #     i
            #     for i, u in enumerate(kilosort_dataset.data["cluster_ids"])
            #     if (kilosort_dataset.data["spike_clusters"] == u).any()
            # ]
            # valid_units = kilosort_dataset.data["cluster_ids"][withspike_idx]
            # valid_unit_labels = kilosort_dataset.data["cluster_groups"][withspike_idx]

            ## ignore remove 0-spike units for now (if they exist..) we'll handle it later on
            valid_units = kilosort_dataset.data["cluster_ids"]
            valid_unit_labels = kilosort_dataset.data["cluster_groups"]
            print('HERE')
            print(len(valid_units), len(valid_unit_labels))

            # -- Spike-times --
            # spike_times_sec_adj > spike_times_sec > spike_times
            spike_time_key = (
                "spike_times_sec_adj"
                if "spike_times_sec_adj" in kilosort_dataset.data
                else (
                    "spike_times_sec"
                    if "spike_times_sec" in kilosort_dataset.data
                    else "spike_times"
                )
            )
            spike_times = kilosort_dataset.data[spike_time_key]
            kilosort_dataset.extract_spike_depths()

            # -- Spike-sites and Spike-depths --
            print('line 1167')
            #print(channel2electrode_map)
            #print(channel2electrode_map["electrode"])
            #print(len(kilosort_dataset.data["spike_sites"]))
            spike_sites = np.array(
                [
                    channel2electrode_map[s]["electrode"]
                    for s in kilosort_dataset.data["spike_sites"]
                ]
            )
            spike_depths = kilosort_dataset.data["spike_depths"]

            print(len(kilosort_dataset.data["spike_depths"]))

            # -- Insert unit, label, peak-chn
            units = []
            print(valid_units)
            print(valid_unit_labels)
            for unit, unit_lbl in zip(valid_units, valid_unit_labels):
                if (kilosort_dataset.data["spike_clusters"] == unit).any():
                    unit_channel, _ = kilosort_dataset.get_best_channel(unit)
                    unit_spike_times = (
                        spike_times[kilosort_dataset.data["spike_clusters"] == unit]
                        / sample_rate
                    )
                    spike_count = len(unit_spike_times)

                    units.append(
                        {
                            **key,
                            "unit": unit,
                            "cluster_quality_label": unit_lbl,
                            **channel2electrode_map[unit_channel],
                            "spike_times": unit_spike_times,
                            "spike_count": spike_count,
                            "spike_sites": spike_sites[
                                kilosort_dataset.data["spike_clusters"] == unit
                            ],
                            "spike_depths": spike_depths[
                                kilosort_dataset.data["spike_clusters"] == unit
                            ],
                        }
                    )
        #print(units)
        self.insert1(key)
        self.Unit.insert(units, ignore_extra_fields=True)


@schema
class WaveformSet(dj.Imported):
    """A set of spike waveforms for units out of a given CuratedClustering.

    Attributes:
        CuratedClustering (foreign key): CuratedClustering primary key.
    """

    definition = """
    # A set of spike waveforms for units out of a given CuratedClustering
    -> CuratedClustering
    """

    class PeakWaveform(dj.Part):
        """Mean waveform across spikes for a given unit.

        Attributes:
            WaveformSet (foreign key): WaveformSet primary key.
            CuratedClustering.Unit (foreign key): CuratedClustering.Unit primary key.
            peak_electrode_waveform (longblob): Mean waveform for a given unit at its representative electrode.
        """

        definition = """
        # Mean waveform across spikes for a given unit at its representative electrode
        -> master
        -> CuratedClustering.Unit
        ---
        peak_electrode_waveform: longblob  # (uV) mean waveform for a given unit at its representative electrode
        """

    class Waveform(dj.Part):
        """Spike waveforms for a given unit.

        Attributes:
            WaveformSet (foreign key): WaveformSet primary key.
            CuratedClustering.Unit (foreign key): CuratedClustering.Unit primary key.
            probe.ElectrodeConfig.Electrode (foreign key): probe.ElectrodeConfig.Electrode primary key.
            waveform_mean (longblob): mean waveform across spikes of the unit in microvolts.
            waveforms (longblob): waveforms of a sampling of spikes at the given electrode and unit.
        """

        definition = """
        # Spike waveforms and their mean across spikes for the given unit
        -> master
        -> CuratedClustering.Unit
        -> probe.ElectrodeConfig.Electrode  
        --- 
        waveform_mean: longblob   # (uV) mean waveform across spikes of the given unit
        waveforms=null: longblob  # (uV) (spike x sample) waveforms of a sampling of spikes at the given electrode for the given unit
        """

    def make(self, key):
        """Populates waveform tables."""
        clustering_method, output_dir = (
            ClusteringTask * ClusteringParamSet & key
        ).fetch1("clustering_method", "clustering_output_dir")
        output_dir = find_full_path(get_ephys_root_data_dir(), output_dir)
        sorter_name = clustering_method.replace(".", "_")

        self.insert1(key)
        if not len(CuratedClustering.Unit & key):
            logger.info(
                f"No CuratedClustering.Unit found for {key}, skipping Waveform ingestion."
            )
            return

        # Get channel and electrode-site mapping
        electrode_query = (EphysRecording.Channel & key).proj(..., "-channel_name")
        channel2electrode_map: dict[int, dict] = {
            chn.pop("channel_idx")+1: chn for chn in electrode_query.fetch(as_dict=True)
        }
        #print('line 1297')
        #print(channel2electrode_map)
        si_sorting_analyzer_dir = output_dir / sorter_name / "sorting_analyzer"
        if si_sorting_analyzer_dir.exists():  # read from spikeinterface outputs
            import spikeinterface as si

            sorting_analyzer = si.load_sorting_analyzer(folder=si_sorting_analyzer_dir)

            # Find representative channel for each unit
            unit_peak_channel: dict[int, np.ndarray] = (
                si.ChannelSparsity.from_best_channels(
                    sorting_analyzer, 1
                ).unit_id_to_channel_indices
            )  # {unit: peak_channel_index}
            unit_peak_channel = {u: chn[0] for u, chn in unit_peak_channel.items()}

            # update channel2electrode_map to match with probe's channel index
            channel2electrode_map = {
                idx: channel2electrode_map[int(chn_idx)]
                for idx, chn_idx in enumerate(sorting_analyzer.get_probe().contact_ids)
            }

            templates = sorting_analyzer.get_extension("templates")

            def yield_unit_waveforms():
                for unit in (CuratedClustering.Unit & key).fetch(
                    "KEY", order_by="unit"
                ):
                    # Get mean waveform for this unit from all channels - (sample x channel)
                    unit_waveforms = templates.get_unit_template(
                        unit_id=unit["unit"], operator="average"
                    )
                    unit_peak_waveform = {
                        **unit,
                        "peak_electrode_waveform": unit_waveforms[
                            :, unit_peak_channel[unit["unit"]]
                        ],
                    }

                    unit_electrode_waveforms = [
                        {
                            **unit,
                            **channel2electrode_map[chn_idx],
                            "waveform_mean": unit_waveforms[:, chn_idx],
                        }
                        for chn_idx in channel2electrode_map
                    ]

                    yield unit_peak_waveform, unit_electrode_waveforms

        else:  # read from kilosort outputs (ecephys pipeline)
            kilosort_dataset = kilosort.Kilosort(output_dir)

            acq_software, probe_serial_number = (
                EphysRecording * ProbeInsertion & key
            ).fetch1("acq_software", "probe")

            # Get all units
            units = {
                u["unit"]: u
                for u in (CuratedClustering.Unit & key).fetch(
                    as_dict=True, order_by="unit"
                )
            }

            if (output_dir / "mean_waveforms.npy").exists():
                unit_waveforms = np.load(
                    output_dir / "mean_waveforms.npy"
                )  # unit x channel x sample

                def yield_unit_waveforms():
                    for unit_no, unit_waveform in zip(
                        kilosort_dataset.data["cluster_ids"], unit_waveforms
                    ):
                        unit_peak_waveform = {}
                        unit_electrode_waveforms = []
                        if unit_no in units:
                            for channel, channel_waveform in zip(
                                kilosort_dataset.data["channel_map"], unit_waveform
                            ):
                                unit_electrode_waveforms.append(
                                    {
                                        **units[unit_no],
                                        **channel2electrode_map[channel],
                                        "waveform_mean": channel_waveform,
                                    }
                                )
                                if (
                                    channel2electrode_map[channel]["electrode"]
                                    == units[unit_no]["electrode"]
                                ):
                                    unit_peak_waveform = {
                                        **units[unit_no],
                                        "peak_electrode_waveform": channel_waveform,
                                    }
                        yield unit_peak_waveform, unit_electrode_waveforms

            else:
                if acq_software == "SpikeGLX":
                    spikeglx_meta_filepath = get_spikeglx_meta_filepath(key)
                    neuropixels_recording = spikeglx.SpikeGLX(
                        spikeglx_meta_filepath.parent
                    )
                elif acq_software == "Open Ephys":
                    session_dir = find_full_path(
                        get_ephys_root_data_dir(), get_session_directory(key)
                    )
                    openephys_dataset = openephys.OpenEphys(session_dir)
                    neuropixels_recording = openephys_dataset.probes[
                        probe_serial_number
                    ]

                def yield_unit_waveforms():
                    #print(units.values())
                    for unit_dict in units.values():
                        unit_peak_waveform = {}
                        unit_electrode_waveforms = []

                        spikes = unit_dict["spike_times"]
                        waveforms = neuropixels_recording.extract_spike_waveforms(
                            spikes, kilosort_dataset.data["channel_map"]
                        )  # (sample x channel x spike)
                        waveforms = waveforms.transpose(
                            (1, 2, 0)
                        )  # (channel x spike x sample)
                        print('line1420')
                        print(len(kilosort_dataset.data["channel_map"]))
                        print(len(waveforms))
                        for channel, channel_waveform in zip(
                            kilosort_dataset.data["channel_map"], waveforms
                        ):
                            unit_electrode_waveforms.append(
                                {
                                    **unit_dict,
                                    **channel2electrode_map[channel],
                                    "waveform_mean": channel_waveform.mean(axis=0),
                                    "waveforms": channel_waveform,
                                }
                            )
                            if (
                                channel2electrode_map[channel]["electrode"]
                                == unit_dict["electrode"]
                            ):
                                unit_peak_waveform = {
                                    **unit_dict,
                                    "peak_electrode_waveform": channel_waveform.mean(
                                        axis=0
                                    ),
                                }

                        yield unit_peak_waveform, unit_electrode_waveforms

        # insert waveform on a per-unit basis to mitigate potential memory issue
        for unit_peak_waveform, unit_electrode_waveforms in yield_unit_waveforms():
            if unit_peak_waveform:
                self.PeakWaveform.insert1(unit_peak_waveform, ignore_extra_fields=True)
            if unit_electrode_waveforms:
                self.Waveform.insert(unit_electrode_waveforms, ignore_extra_fields=True)


@schema
class QualityMetrics(dj.Imported):
    """Clustering and waveform quality metrics.

    Attributes:
        CuratedClustering (foreign key): CuratedClustering primary key.
    """

    definition = """
    # Clusters and waveforms metrics
    -> CuratedClustering    
    """

    class Cluster(dj.Part):
        """Cluster metrics for a unit.

        Attributes:
            QualityMetrics (foreign key): QualityMetrics primary key.
            CuratedClustering.Unit (foreign key): CuratedClustering.Unit primary key.
            firing_rate (float): Firing rate of the unit.
            snr (float): Signal-to-noise ratio for a unit.
            presence_ratio (float): Fraction of time where spikes are present.
            isi_violation (float): rate of ISI violation as a fraction of overall rate.
            number_violation (int): Total ISI violations.
            amplitude_cutoff (float): Estimate of miss rate based on amplitude histogram.
            isolation_distance (float): Distance to nearest cluster.
            l_ratio (float): Amount of empty space between a cluster and other spikes in dataset.
            d_prime (float): Classification accuracy based on LDA.
            nn_hit_rate (float): Fraction of neighbors for target cluster that are also in target cluster.
            nn_miss_rate (float): Fraction of neighbors outside target cluster that are in the target cluster.
            silhouette_core (float): Maximum change in spike depth throughout recording.
            cumulative_drift (float): Cumulative change in spike depth throughout recording.
            contamination_rate (float): Frequency of spikes in the refractory period.
        """

        definition = """   
        # Cluster metrics for a particular unit
        -> master
        -> CuratedClustering.Unit
        ---
        firing_rate=null: float # (Hz) firing rate for a unit 
        snr=null: float  # signal-to-noise ratio for a unit
        presence_ratio=null: float  # fraction of time in which spikes are present
        isi_violation=null: float   # rate of ISI violation as a fraction of overall rate
        number_violation=null: int  # total number of ISI violations
        amplitude_cutoff=null: float  # estimate of miss rate based on amplitude histogram
        isolation_distance=null: float  # distance to nearest cluster in Mahalanobis space
        l_ratio=null: float  # 
        d_prime=null: float  # Classification accuracy based on LDA
        nn_hit_rate=null: float  # Fraction of neighbors for target cluster that are also in target cluster
        nn_miss_rate=null: float # Fraction of neighbors outside target cluster that are in target cluster
        silhouette_score=null: float  # Standard metric for cluster overlap
        max_drift=null: float  # Maximum change in spike depth throughout recording
        cumulative_drift=null: float  # Cumulative change in spike depth throughout recording 
        contamination_rate=null: float # 
        """

    class Waveform(dj.Part):
        """Waveform metrics for a particular unit.

        Attributes:
            QualityMetrics (foreign key): QualityMetrics primary key.
            CuratedClustering.Unit (foreign key): CuratedClustering.Unit primary key.
            amplitude (float): Absolute difference between waveform peak and trough in microvolts.
            duration (float): Time between waveform peak and trough in milliseconds.
            halfwidth (float): Spike width at half max amplitude.
            pt_ratio (float): Absolute amplitude of peak divided by absolute amplitude of trough relative to 0.
            repolarization_slope (float): Slope of the regression line fit to first 30 microseconds from trough to peak.
            recovery_slope (float): Slope of the regression line fit to first 30 microseconds from peak to tail.
            spread (float): The range with amplitude over 12-percent of maximum amplitude along the probe.
            velocity_above (float): inverse velocity of waveform propagation from soma to the top of the probe.
            velocity_below (float): inverse velocity of waveform propagation from soma toward the bottom of the probe.
        """

        definition = """   
        # Waveform metrics for a particular unit
        -> master
        -> CuratedClustering.Unit
        ---
        amplitude=null: float  # (uV) absolute difference between waveform peak and trough
        duration=null: float  # (ms) time between waveform peak and trough
        halfwidth=null: float  # (ms) spike width at half max amplitude
        pt_ratio=null: float  # absolute amplitude of peak divided by absolute amplitude of trough relative to 0
        repolarization_slope=null: float  # the repolarization slope was defined by fitting a regression line to the first 30us from trough to peak
        recovery_slope=null: float  # the recovery slope was defined by fitting a regression line to the first 30us from peak to tail
        spread=null: float  # (um) the range with amplitude above 12-percent of the maximum amplitude along the probe
        velocity_above=null: float  # (s/m) inverse velocity of waveform propagation from the soma toward the top of the probe
        velocity_below=null: float  # (s/m) inverse velocity of waveform propagation from the soma toward the bottom of the probe
        """

    def make(self, key):
        """Populates tables with quality metrics data."""
        # Load metrics.csv
        clustering_method, output_dir = (
            ClusteringTask * ClusteringParamSet & key
        ).fetch1("clustering_method", "clustering_output_dir")
        output_dir = find_full_path(get_ephys_root_data_dir(), output_dir)
        sorter_name = clustering_method.replace(".", "_")

        self.insert1(key)
        if not len(CuratedClustering.Unit & key):
            logger.info(
                f"No CuratedClustering.Unit found for {key}, skipping QualityMetrics ingestion."
            )
            return

        si_sorting_analyzer_dir = output_dir / sorter_name / "sorting_analyzer"
        if si_sorting_analyzer_dir.exists():  # read from spikeinterface outputs
            import spikeinterface as si

            sorting_analyzer = si.load_sorting_analyzer(folder=si_sorting_analyzer_dir)
            qc_metrics = sorting_analyzer.get_extension("quality_metrics").get_data()
            template_metrics = sorting_analyzer.get_extension(
                "template_metrics"
            ).get_data()
            metrics_df = pd.concat([qc_metrics, template_metrics], axis=1)

            metrics_df.rename(
                columns={
                    "amplitude_median": "amplitude",
                    "isi_violations_ratio": "isi_violation",
                    "isi_violations_count": "number_violation",
                    "silhouette": "silhouette_score",
                    "rp_contamination": "contamination_rate",
                    "drift_ptp": "max_drift",
                    "drift_mad": "cumulative_drift",
                    "half_width": "halfwidth",
                    "peak_trough_ratio": "pt_ratio",
                    "peak_to_valley": "duration",
                },
                inplace=True,
            )
        else:  # read from kilosort outputs (ecephys pipeline)
            # find metric_fp
            for metric_fp in [
                output_dir / "metrics.csv",
            ]:
                if metric_fp.exists():
                    break
            else:
                raise FileNotFoundError(f"QC metrics file not found in: {output_dir}")

            metrics_df = pd.read_csv(metric_fp)

            # Conform the dataframe to match the table definition
            if "cluster_id" in metrics_df.columns:
                metrics_df.set_index("cluster_id", inplace=True)
            else:
                metrics_df.rename(
                    columns={metrics_df.columns[0]: "cluster_id"}, inplace=True
                )
                metrics_df.set_index("cluster_id", inplace=True)

            metrics_df.columns = metrics_df.columns.str.lower()

        metrics_df.replace([np.inf, -np.inf], np.nan, inplace=True)
        metrics_list = [
            dict(metrics_df.loc[unit_key["unit"]], **unit_key)
            for unit_key in (CuratedClustering.Unit & key).fetch("KEY")
        ]

        self.Cluster.insert(metrics_list, ignore_extra_fields=True)
        self.Waveform.insert(metrics_list, ignore_extra_fields=True)


# ---------------- HELPER FUNCTIONS ----------------


def get_spikeglx_meta_filepath(ephys_recording_key: dict) -> str:
    """Get spikeGLX data filepath."""
    # attempt to retrieve from EphysRecording.EphysFile
    spikeglx_meta_filepath = pathlib.Path(
        (
            EphysRecording.EphysFile
            & ephys_recording_key
            & 'file_path LIKE "%.ap.meta"'
        ).fetch1("file_path")
    )

    try:
        spikeglx_meta_filepath = find_full_path(
            get_ephys_root_data_dir(), spikeglx_meta_filepath
        )
    except FileNotFoundError:
        # if not found, search in session_dir again
        if not spikeglx_meta_filepath.exists():
            session_dir = find_full_path(
                get_ephys_root_data_dir(), get_session_directory(ephys_recording_key)
            )
            inserted_probe_serial_number = (
                ProbeInsertion * probe.Probe & ephys_recording_key
            ).fetch1("probe")

            spikeglx_meta_filepaths = [fp for fp in session_dir.rglob("*.ap.meta")]
            for meta_filepath in spikeglx_meta_filepaths:
                spikeglx_meta = spikeglx.SpikeGLXMeta(meta_filepath)
                if str(spikeglx_meta.probe_SN) == inserted_probe_serial_number:
                    spikeglx_meta_filepath = meta_filepath
                    break
            else:
                raise FileNotFoundError(
                    "No SpikeGLX data found for probe insertion: {}".format(
                        ephys_recording_key
                    )
                )

    return spikeglx_meta_filepath


def get_openephys_probe_data(ephys_recording_key: dict) -> list:
    """Get OpenEphys probe data from file."""
    inserted_probe_serial_number = (
        ProbeInsertion * probe.Probe & ephys_recording_key
    ).fetch1("probe")
    session_dir = find_full_path(
        get_ephys_root_data_dir(), get_session_directory(ephys_recording_key)
    )
    loaded_oe = openephys.OpenEphys(session_dir)
    probe_data = loaded_oe.probes[inserted_probe_serial_number]

    # explicitly garbage collect "loaded_oe"
    # as these may have large memory footprint and may not be cleared fast enough
    del loaded_oe
    gc.collect()

    return probe_data


def get_recording_channels_details(ephys_recording_key: dict) -> np.array:
    """Get details of recording channels for a given recording."""
    channels_details = {}

    acq_software, sample_rate = (EphysRecording & ephys_recording_key).fetch1(
        "acq_software", "sampling_rate"
    )

    probe_type = (ProbeInsertion * probe.Probe & ephys_recording_key).fetch1(
        "probe_type"
    )
    channels_details["probe_type"] = {
        "neuropixels 1.0 - 3A": "3A",
        "neuropixels 1.0 - 3B": "NP1",
        "neuropixels UHD": "NP1100",
        "neuropixels 2.0 - SS": "NP21",
        "neuropixels 2.0 - MS": "NP24",
    }[probe_type]

    electrode_config_key = (
        probe.ElectrodeConfig * EphysRecording & ephys_recording_key
    ).fetch1("KEY")
    (
        channels_details["channel_ind"],
        channels_details["x_coords"],
        channels_details["y_coords"],
        channels_details["shank_ind"],
    ) = (
        probe.ElectrodeConfig.Electrode * probe.ProbeType.Electrode
        & electrode_config_key
    ).fetch(
        "electrode", "x_coord", "y_coord", "shank"
    )
    channels_details["sample_rate"] = sample_rate
    channels_details["num_channels"] = len(channels_details["channel_ind"])

    if acq_software == "SpikeGLX":
        spikeglx_meta_filepath = get_spikeglx_meta_filepath(ephys_recording_key)
        spikeglx_recording = spikeglx.SpikeGLX(spikeglx_meta_filepath.parent)
        channels_details["uVPerBit"] = spikeglx_recording.get_channel_bit_volts("ap")[0]
        channels_details["connected"] = np.array(
            [v for *_, v in spikeglx_recording.apmeta.shankmap["data"]]
        )
    elif acq_software == "Open Ephys":
        oe_probe = get_openephys_probe_data(ephys_recording_key)
        channels_details["uVPerBit"] = oe_probe.ap_meta["channels_gains"][0]
        channels_details["connected"] = np.array(
            [
                int(v == 1)
                for c, v in oe_probe.channels_connected.items()
                if c in channels_details["channel_ind"]
            ]
        )

    return channels_details


def generate_electrode_config_entry(probe_type: str, electrode_keys: list) -> dict:
    """Generate and insert new ElectrodeConfig

    Args:
        probe_type (str): probe type (e.g. neuropixels 2.0 - SS)
        electrode_keys (list): list of keys of the probe.ProbeType.Electrode table

    Returns:
        dict: representing a key of the probe.ElectrodeConfig table
    """
    # compute hash for the electrode config (hash of dict of all ElectrodeConfig.Electrode)
    electrode_config_hash = dict_to_uuid({k["electrode"]: k for k in electrode_keys})

    electrode_list = sorted([k["electrode"] for k in electrode_keys])
    electrode_gaps = (
        [-1]
        + np.where(np.diff(electrode_list) > 1)[0].tolist()
        + [len(electrode_list) - 1]
    )
    electrode_config_name = "; ".join(
        [
            f"{electrode_list[start + 1]}-{electrode_list[end]}"
            for start, end in zip(electrode_gaps[:-1], electrode_gaps[1:])
        ]
    )
    electrode_config_key = {"electrode_config_hash": electrode_config_hash}
    econfig_entry = {
        **electrode_config_key,
        "probe_type": probe_type,
        "electrode_config_name": electrode_config_name,
    }
    econfig_electrodes = [
        {**electrode, **electrode_config_key} for electrode in electrode_keys
    ]

    return econfig_entry, econfig_electrodes
