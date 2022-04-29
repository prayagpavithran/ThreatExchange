let ffmpeg = {};
// Execute the following codes once the DOM contents are loaded completely.
document.addEventListener("DOMContentLoaded", (event) => {
	const { createFFmpeg, fetchFile } = FFmpeg;
	// create the FFmpeg instance and load it
	ffmpeg = createFFmpeg({ log: true });

	const loadFFMpeg = async () => {
		await ffmpeg.load();
		document.getElementById("uploadFiles").classList.remove("disabled");

	}

	loadFFMpeg();

	document.getElementById("uploadFiles").onclick = function () {
		document.getElementById("uploadFiles").classList.add("upload--loading");
		document.getElementsByClassName("upload-hidden")[0].click();
	};

	document.getElementById("myfile").onchange = function () {
		const file = document.getElementById("myfile").files[0];
		console.log('foe', file);
		document.getElementById("uploadFiles").classList.add("disabled");
		document.getElementById("uploadFiles").classList.remove("upload--loading");
		document.getElementsByTagName("BODY")[0].classList.add('file-process-open');

		let fname = '';
		let tmkfname = '';
		let reader = new FileReader();

		if (file) {
			fname = file.name;
			if (fname) {
				tmkfname = `${fname.substr(0, fname.lastIndexOf("."))}.tmk`;
			}
		}

		reader.onloadend = function (e) {

			let result = reader.result;
			const data = new Uint8Array(result);

			// Save the selected files to emscripten file system for c++ code to access the file.
			let stream = FS.open(fname, 'w+');
			FS.write(stream, data, 0, data.length, 0);
			FS.close(stream);

			// Call the function for running FFMPEG wasm for converting video file to .rgb.
			runFFMpeg(fname, tmkfname);
		}

		reader.readAsArrayBuffer(file);
	}

});

const runFFMpeg = async function (filename, tmkfilename) {

	const rgbFileName = "output.rgb";

	// Read the file whose hash needs to be calculated.     
	let sourceBytes = FS.readFile(filename);

	// write the mp4 to the FFmpeg file system
	ffmpeg.FS(
		"writeFile",
		filename,
		sourceBytes
	);


	// run the FFmpeg command-line tool, converting the MP4 into rgb
	await ffmpeg.run("-nostdin", "-i", filename, "-s", "64:64", "-an", "-f", "rawvideo", "-c:v", "rawvideo", "-pix_fmt", "rgb24", "-r", "15", rgbFileName);
	// Remove the input file saved in ffmpeg memory
	ffmpeg.FS("unlink", filename);

	// read the MP4 file back from the FFmpeg file system
	const output = ffmpeg.FS("readFile", rgbFileName);
	// Save the .rgb file to emscripten file system.		        
	let stream = FS.open(rgbFileName, 'w+');
	FS.write(stream, output, 0, output.length, 0);
	FS.close(stream);

	console.log("calling the get video hash method");
	var result = Module.ccall(
		'getVideoHash',	// name of C function
		'number',	// return type
		['string'],	// argument types
		[tmkfilename]	// arguments
	);

	if (result == 1) {
		saveTMKFile(tmkfilename);
	}
	else {
		alert('Error occured while generating .tmk  files');
	}
	document.getElementById("uploadFiles").classList.add("disabled");
}

const saveTMKFile = (function (tmkfilename) {
	var a = document.createElement("a");
	document.body.appendChild(a);
	a.style = "display: none";
	const blob = new Blob([FS.readFile(tmkfilename)]);
	const url = URL.createObjectURL(blob);
	a.href = url;
	a.download = tmkfilename;
	a.click();
	a.class = "download";
	window.URL.revokeObjectURL(url);

	// Remove the file so that we can free some memory.
	FS.unlink(tmkfilename);
});